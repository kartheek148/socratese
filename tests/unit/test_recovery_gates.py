"""M6.4 Phase 1 - recovery evidence gates: provenance, freshness, the gate result contract and the
constraints that make recovery impossible without an explicitly passing gate evaluation. Every
gate is tested failing on its own, failing closed on missing data, and deterministic."""

import json
import random
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError
from test_drift_monitor import ALT, T0, ref
from test_revalidation_core import (
    CELL,
    S,
    U,
    degrade,
    drive,
    hours,
    new_fleet,
    open_case,
    rp,
    triggers,
)

from experience_engine.controller import revalidation as reval
from experience_engine.controller.recovery_gates import (
    GateContext,
    RecoveryGateViolation,
    admissibility_problems,
    evaluate_gates,
    preflight,
)
from experience_engine.controller.revalidation import StepOutput
from experience_engine.domain.recovery import (
    GATES_VERSION,
    GateId,
    GateReport,
    GateResult,
    RecoveryPolicy,
    TrialProvenance,
    VerificationStatus,
)
from experience_engine.domain.revalidation import (
    ExecutorFacts,
    RevalState,
    RevalTransition,
    RevalTrial,
    RevalTrigger,
    TrialKey,
    TrialResult,
    TrialStage,
    TrialVerdict,
)
from recovery_support import POLICY, AttestedScript, provenance

CFG = "a" * 64
OBS = T0 + hours(20)
DECISION = T0 + hours(30)
TO = TrialVerdict.TIMEOUT
ALL = {g.value for g in GateId}


def trial(
    i: int,
    stage: TrialStage = TrialStage.EVIDENCE,
    *,
    prov: dict[str, Any] | None = None,
    status: VerificationStatus | None = VerificationStatus.VERIFIED,
    **over: Any,
) -> RevalTrial:
    base: dict[str, Any] = {
        "verdict": S, "tokens": 100, "task_ref": f"t{i}-{stage.value}",
        "observed_at": OBS + timedelta(minutes=i), "runtime_version": "rt-1",
        "verifier_version": "v-1", "capability_ref": ALT, "tenant_id": "bench",
    }  # fmt: skip
    result = TrialResult(
        **{**base, **over},
        provenance=provenance(i + (1000 if stage.value[0] == "C" else 0), status, **(prov or {})),
    )
    key = TrialKey(cell_key=CELL, episode=1, cycle=0, ordinal=i, stage=stage)
    return RevalTrial(key=key, result=result, anchor="a", accepted=True)


def good_set(first: RevalTrial | None = None, n: int = 3) -> list[RevalTrial]:
    evidence = [first or trial(0), *[trial(i) for i in range(1, n)]]
    return [*evidence, trial(0, TrialStage.CONFIRMATION)]


def ctx(**over: Any) -> GateContext:
    base = GateContext(
        policy=POLICY, tenant_id="bench", cell_key=CELL, episode=1, cycle=0, family="timeout",
        risk="low", alt_key=ALT, ref_runtime="rt-1", trigger_at=T0, decision_at=DECISION,
        config_hash=CFG, confirm_required=1,
    )  # fmt: skip
    return replace(base, **over)


def failed(report: GateReport) -> set[str]:
    return set(report.failed_gates)


# ---- the happy path and the contract -------------------------------------------------------------
def test_complete_and_consistent_evidence_passes_every_gate() -> None:
    report = evaluate_gates(ctx(), good_set())
    assert report.eligible and report.failed_gates == []
    assert report.passed_gates == [g.value for g in GateId]  # every successful gate is recorded
    assert report.policy_version == GATES_VERSION == "m6.4-gates-v1"
    assert len(report.evidence_ids) == 4 and len(set(report.evidence_ids)) == 4
    assert report.config_hash == CFG and report.evaluated_at == DECISION
    assert [r.gate for r in report.results] == list(GateId)
    assert all(r.checked == 4 and r.passed and not r.reasons for r in report.results)


def test_the_report_has_the_documented_json_shape_and_round_trips() -> None:
    payload = evaluate_gates(ctx(), good_set()).model_dump(mode="json")
    assert {"eligible", "policy_version", "failed_gates", "evidence_ids"} <= set(payload)
    json.dumps(payload)  # plain JSON: it is stored in a JSONB column
    assert GateReport.model_validate(payload).model_dump(mode="json") == payload
    bad = evaluate_gates(ctx(), [])
    assert not bad.eligible and bad.evidence_ids == []
    assert GateReport.model_validate(bad.model_dump(mode="json")) == bad


def test_eligibility_is_derived_so_a_payload_cannot_assert_it() -> None:
    ok = evaluate_gates(ctx(), good_set()).model_dump(mode="json")
    for patch in (
        {"failed_gates": ["evidence_freshness"]},  # does not match the results
        {"passed_gates": ok["passed_gates"][:-1]},
        {"evidence_ids": []},  # eligible without a single evidence reference
        {"policy_version": ""},  # eligible without a policy version
        {"results": ok["results"][:-1]},  # a gate missing from the evaluation
    ):
        with pytest.raises(ValidationError):
            GateReport.model_validate({**ok, **patch})
    nope = evaluate_gates(ctx(), [])
    with pytest.raises(ValidationError):
        GateReport.model_validate({**nope.model_dump(mode="json"), "eligible": True})
    with pytest.raises(ValidationError):  # a passing gate cannot carry a failure and vice versa
        GateResult(gate=GateId.EVIDENCE_PRESENCE, passed=True, checked=1, reasons=["x"])
    with pytest.raises(ValidationError):
        GateResult(gate=GateId.EVIDENCE_PRESENCE, passed=False, checked=1)


def test_the_evaluation_is_deterministic_and_independent_of_the_input_order() -> None:
    items = good_set()
    first = evaluate_gates(ctx(), items)
    shuffled = items[:]
    random.Random(3).shuffle(shuffled)
    assert evaluate_gates(ctx(), shuffled) == first == evaluate_gates(ctx(), items)
    assert evaluate_gates(ctx(), items).model_dump_json() == first.model_dump_json()


def test_no_gate_short_circuits_so_every_failure_and_every_pass_is_recorded() -> None:
    bad = trial(0, prov={"executor_id": "rogue", "tool_versions": {"sql": "9"}},
                capability_ref="DIRECT_CAPABILITY:other@1")  # fmt: skip
    report = evaluate_gates(ctx(), good_set(bad))
    assert failed(report) == {"capability_identity", "executor_compatibility", "tool_compatibility"}
    assert set(report.passed_gates) == ALL - failed(report)
    assert report.failed_gates == [g.value for g in GateId if g.value in failed(report)]
    assert report.results[1].failed_evidence == ["bench|e1|c0|E0".replace("bench", CELL)]


def test_the_empty_evidence_set_fails_every_gate_and_never_passes_vacuously() -> None:
    report = evaluate_gates(ctx(), [])
    assert failed(report) == ALL and not report.eligible and report.passed_gates == []
    assert all("no evidence" in r.reasons[0] for r in report.results)


# ---- every gate fails on its own -----------------------------------------------------------------
G = GateId
CASES: list[tuple[str, dict[str, Any], dict[str, Any], set[GateId]]] = [
    # name, trial result fields, provenance fields, gates that must fail
    ("capability of another version", {"capability_ref": "DIRECT_CAPABILITY:cap@2"}, {},
     {G.CAPABILITY_IDENTITY}),
    ("capability not attested", {"capability_ref": None}, {}, {G.CAPABILITY_IDENTITY}),
    ("another tenant", {"tenant_id": "other"}, {}, {G.CONTEXT_ISOLATION}),
    ("tenant not attested", {"tenant_id": None}, {}, {G.CONTEXT_ISOLATION}),
    ("another family", {}, {"task_family": "queue"}, {G.CONTEXT_ISOLATION}),
    ("family not attested", {}, {"task_family": None}, {G.CONTEXT_ISOLATION}),
    ("another risk class", {}, {"risk_level": "high"}, {G.CONTEXT_ISOLATION}),
    ("risk class not attested", {}, {"risk_level": None}, {G.CONTEXT_ISOLATION}),
    ("executor not accepted", {}, {"executor_id": "rogue"}, {G.EXECUTOR_COMPATIBILITY}),
    ("executor not attested", {}, {"executor_id": None}, {G.EXECUTOR_COMPATIBILITY}),
    ("runtime not validated", {"runtime_version": "rt-9"}, {}, {G.RUNTIME_COMPATIBILITY}),
    ("runtime not attested", {"runtime_version": None}, {}, {G.RUNTIME_COMPATIBILITY}),
    ("another model", {}, {"model_id": "model-z"}, {G.RUNTIME_COMPATIBILITY}),
    ("model not attested", {}, {"model_id": None}, {G.RUNTIME_COMPATIBILITY}),
    ("model version not accepted", {}, {"model_version": "9"}, {G.RUNTIME_COMPATIBILITY}),
    ("model version not attested", {}, {"model_version": None}, {G.RUNTIME_COMPATIBILITY}),
    ("tool version not accepted", {}, {"tool_versions": {"sql": "9"}}, {G.TOOL_COMPATIBILITY}),
    ("unknown tool", {}, {"tool_versions": {"shell": "1"}}, {G.TOOL_COMPATIBILITY}),
    ("tools not attested", {}, {"tool_versions": None}, {G.TOOL_COMPATIBILITY}),
    ("verifier not accepted", {}, {"verifier_id": "rogue"}, {G.VERIFIER_INTEGRITY}),
    ("verifier not attested", {}, {"verifier_id": None}, {G.VERIFIER_INTEGRITY}),
    ("verifier version not accepted", {"verifier_version": "v-9"}, {}, {G.VERIFIER_INTEGRITY}),
    ("verifier version not attested", {"verifier_version": None}, {}, {G.VERIFIER_INTEGRITY}),
    ("verifier independence not attested", {}, {"verifier_independent": None},
     {G.VERIFIER_INTEGRITY}),
    ("verifier not independent", {}, {"verifier_independent": False}, {G.VERIFIER_INTEGRITY}),
    ("unattributed timeout", {"verdict": TO}, {}, {G.VERIFIED_OUTCOME}),
    ("verification status not attested", {}, {"verification_status": None}, {G.VERIFIED_OUTCOME}),
    ("not verified", {}, {"verification_status": VerificationStatus.UNAVAILABLE},
     {G.VERIFIED_OUTCOME}),
    ("evidence source not accepted", {}, {"evidence_source": "rogue"}, {G.LINEAGE_INTEGRITY}),
    ("evidence source not attested", {}, {"evidence_source": None}, {G.LINEAGE_INTEGRITY}),
    ("execution not attested", {}, {"execution_id": None}, {G.LINEAGE_INTEGRITY}),
    ("task pool not attested", {}, {"pool_id": None}, {G.LINEAGE_INTEGRITY}),
    ("task reference missing", {"task_ref": ""}, {}, {G.LINEAGE_INTEGRITY}),
    ("not newer than the quarantine", {"observed_at": T0}, {}, {G.LINEAGE_INTEGRITY}),
]  # fmt: skip


@pytest.mark.parametrize(("name", "fields", "prov", "gates"), CASES, ids=[c[0] for c in CASES])
def test_each_gate_fails_on_its_own_and_says_why(
    name: str, fields: dict[str, Any], prov: dict[str, Any], gates: set[GateId]
) -> None:
    report = evaluate_gates(ctx(), good_set(trial(0, prov=prov, **fields)))
    assert not report.eligible, name
    assert failed(report) == {g.value for g in gates}, name
    for result in report.results:
        if result.gate in gates:
            assert result.reasons or result.failed_evidence


def test_a_non_evidence_attempt_in_the_set_fails_presence_and_outcome() -> None:
    bad = trial(0, verdict=U, status=VerificationStatus.UNAVAILABLE)
    report = evaluate_gates(ctx(), good_set(bad))
    assert failed(report) == {"evidence_presence", "verified_outcome"}


def test_missing_provenance_fails_closed_everywhere_it_is_needed() -> None:
    blank = [
        RevalTrial(
            key=t.key, anchor="a", accepted=True,
            result=t.result.model_copy(update={"provenance": TrialProvenance()}),
        )
        for t in good_set()
    ]  # fmt: skip
    report = evaluate_gates(ctx(), blank)
    assert not report.eligible
    assert failed(report) == {
        "context_isolation", "executor_compatibility", "runtime_compatibility",
        "tool_compatibility", "verifier_integrity", "verified_outcome", "lineage_integrity",
    }  # fmt: skip
    # what the trial row itself carries still passes: nothing was invented to fill the gaps
    assert {"capability_identity", "evidence_freshness", "policy_integrity"} <= set(
        report.passed_gates
    )


def test_an_empty_tool_list_is_an_explicit_statement_and_is_valid() -> None:
    assert evaluate_gates(ctx(), good_set(trial(0, prov={"tool_versions": {}}))).eligible


def test_the_evidence_set_must_be_consistent_not_only_each_trial() -> None:
    cases = [
        ({"executor_id": "exec-2"}, {}, "executor"),
        ({"model_version": "2"}, {}, "model version"),
        ({"model_id": "model-b"}, {}, "model"),
        ({}, {"runtime_version": "rt-1b"}, "runtime version"),
    ]
    pol = POLICY.model_copy(
        update={
            "accepted_executors": ("exec-1", "exec-2"),
            "accepted_models": {"model-a": ("1", "2"), "model-b": ("1",)},
            "validated_runtimes": ("rt-1", "rt-1b"),
        }
    )
    assert evaluate_gates(ctx(policy=pol), good_set()).eligible
    for prov, fields, label in cases:
        mixed = [good_set()[0], trial(1, prov=prov, **fields), *good_set()[2:]]
        report = evaluate_gates(ctx(policy=pol), mixed)
        assert not report.eligible, label
        reasons = " ".join(x for r in report.results for x in r.reasons)
        assert f"{label} changed within the evidence set" in reasons


def test_the_verifier_must_be_independent_of_the_executor_and_the_model() -> None:
    pol = POLICY.model_copy(
        update={"accepted_verifiers": {"verifier-x": ("v-1",), "exec-1": ("v-1",)}}
    )
    report = evaluate_gates(ctx(policy=pol), good_set(trial(0, prov={"verifier_id": "exec-1"})))
    assert failed(report) == {"verifier_integrity"}
    assert "independent" in " ".join(report.results[6].reasons)


def test_lineage_rejects_foreign_cases_and_duplicates() -> None:
    items = good_set()
    other_cycle = RevalTrial(key=items[1].key.model_copy(update={"cycle": 1}), anchor="a",
                             accepted=True, result=items[1].result)  # fmt: skip
    assert "lineage_integrity" in failed(evaluate_gates(ctx(), [items[0], other_cycle, *items[2:]]))
    twin = RevalTrial(key=items[1].key.model_copy(update={"ordinal": 77}), anchor="a",
                      accepted=True, result=items[1].result)  # fmt: skip
    report = evaluate_gates(ctx(), [*items, twin])  # the same task and execution delivered twice
    lineage = report.results[9]
    assert not lineage.passed and "duplicate task" in " ".join(lineage.reasons)
    assert "duplicate execution" in " ".join(lineage.reasons) and len(lineage.failed_evidence) >= 2
    assert not evaluate_gates(ctx(trigger_at=None), items).eligible  # newer-than-what is unknown


def test_a_rejected_trial_is_never_part_of_the_evidence() -> None:
    items = good_set()
    rejected = RevalTrial(
        key=items[0].key, anchor="a", accepted=False, result=items[0].result,
        reject_reason=reval.RejectReason.DUPLICATE,
    )  # fmt: skip
    assert failed(evaluate_gates(ctx(), [rejected, *items[1:]])) == {"evidence_presence"}


def test_required_confirmation_evidence_must_be_present() -> None:
    assert failed(evaluate_gates(ctx(), good_set()[:-1])) == {"evidence_presence"}
    assert evaluate_gates(ctx(confirm_required=0), good_set()[:-1]).eligible


# ---- policy integrity ----------------------------------------------------------------------------
def test_the_gate_policy_must_be_a_supported_explicit_version_and_bound_to_a_configuration() -> (
    None
):
    for pol, cfg, why in (
        (POLICY.model_copy(update={"gates_version": ""}), CFG, "missing"),
        (POLICY.model_copy(update={"gates_version": "m6.3-gates-v0"}), CFG, "unsupported"),
        (POLICY, "", "hash"),
        (POLICY, "xyz", "hash"),
    ):
        report = evaluate_gates(ctx(policy=pol, config_hash=cfg), good_set())
        assert failed(report) == {"policy_integrity"} and not report.eligible, why


def test_an_empty_allow_list_accepts_nothing() -> None:
    nothing = RecoveryPolicy()
    report = evaluate_gates(ctx(policy=nothing), good_set())
    assert {"executor_compatibility", "runtime_compatibility", "tool_compatibility",
            "verifier_integrity", "lineage_integrity"} <= failed(report)  # fmt: skip
    assert not report.eligible


# ---- freshness -----------------------------------------------------------------------------------
def aged(age_hours: float, **over: Any) -> list[RevalTrial]:
    at = DECISION - timedelta(hours=age_hours)
    return good_set(trial(0, observed_at=at, **over))


def fresh(report: GateReport) -> bool:
    return GateId.EVIDENCE_FRESHNESS.value not in report.failed_gates


def test_fresh_evidence_passes_and_the_window_is_frozen_at_120_hours() -> None:
    assert POLICY.evidence_window_hours == 120.0
    assert fresh(evaluate_gates(ctx(trigger_at=DECISION - hours(500)), aged(10)))


@pytest.mark.parametrize(
    ("delta_s", "ok"), [(-3600, True), (-1, True), (0, True), (1, False), (3600, False)]
)
def test_evidence_at_the_window_boundary_is_decided_deterministically(
    delta_s: int, ok: bool
) -> None:
    c = ctx(trigger_at=DECISION - hours(1000))
    items = good_set(trial(0, observed_at=DECISION - timedelta(seconds=120 * 3600 + delta_s)))
    assert fresh(evaluate_gates(c, items)) is ok  # exactly 120 h is inside, one second more is not


def test_expired_evidence_fails_and_names_the_trial() -> None:
    report = evaluate_gates(ctx(trigger_at=DECISION - hours(1000)), aged(200))
    result = next(r for r in report.results if r.gate == GateId.EVIDENCE_FRESHNESS)
    assert not result.passed
    assert len(result.failed_evidence) == 1 and "120" in result.reasons[0]
    assert not report.eligible


def test_a_missing_timestamp_fails() -> None:
    report = evaluate_gates(ctx(), good_set(trial(0, observed_at=datetime.min)))
    assert {"evidence_freshness", "lineage_integrity"} <= failed(report)
    why = " ".join(x for r in report.results for x in r.reasons)
    assert why.count("timestamp missing") == 2  # named as missing, not as merely old or foreign
    unstamped_aware = datetime.min.replace(tzinfo=UTC)
    assert "evidence_freshness" in failed(
        evaluate_gates(ctx(decision_at=DECISION), good_set(trial(0, observed_at=unstamped_aware)))
    )


def test_a_future_timestamp_fails_and_is_not_tolerated() -> None:
    c = ctx()
    assert not fresh(
        evaluate_gates(c, good_set(trial(0, observed_at=DECISION + timedelta(seconds=1))))
    )
    assert fresh(evaluate_gates(c, good_set(trial(0, observed_at=DECISION))))  # now is not future


def test_an_unknown_decision_time_or_mixed_timezones_cannot_establish_freshness() -> None:
    assert not fresh(evaluate_gates(ctx(decision_at=None), good_set()))
    naive_decision = ctx(decision_at=datetime(2026, 1, 2), trigger_at=datetime(2025, 12, 1))
    assert not fresh(evaluate_gates(naive_decision, good_set()))  # aware trials, naive clock


def test_the_window_is_a_policy_parameter_and_part_of_its_hash() -> None:
    tight = POLICY.model_copy(update={"evidence_window_hours": 24.0})
    c = ctx(policy=tight, trigger_at=DECISION - hours(1000))
    assert fresh(evaluate_gates(c, aged(24.0))) and not fresh(evaluate_gates(c, aged(24.5)))


# ---- recovery constraints ------------------------------------------------------------------------
def payload(**patch: Any) -> dict[str, Any]:
    return {**evaluate_gates(ctx(), good_set()).model_dump(mode="json"), **patch}


def test_a_passing_report_authorises_a_recovery_and_only_a_passing_one_does() -> None:
    assert admissibility_problems(POLICY, CFG, payload()) == []
    assert admissibility_problems(None, CFG, {}) == []  # M6.3 policies have no gate to require


def test_no_evaluation_a_failing_evaluation_or_an_unidentified_one_cannot_recover() -> None:
    failing = evaluate_gates(ctx(), good_set(trial(0, prov={"executor_id": "rogue"})))
    cases: dict[str, dict[str, Any]] = {
        "no gate evaluation": {},
        "gates failed": failing.model_dump(mode="json"),
        "malformed": payload(eligible=True, policy_version=""),  # no policy version
        "malformed2": payload(eligible=True, evidence_ids=[]),  # no evidence references
        "other configuration": payload(config_hash="b" * 64),
        "other gate policy version": payload(policy_version="m6.4-gates-v0"),
        "garbage": {"eligible": True},
    }
    for name, data in cases.items():
        assert admissibility_problems(POLICY, CFG, data), name
    # a merely populated payload is not enough: every key present, still not authorised
    assert admissibility_problems(POLICY, CFG, {**payload(), "eligible": False})


def test_a_failed_gate_consumes_the_cycle_and_never_recovers() -> None:
    p = rp(recovery=POLICY, max_cycles=2)
    src = AttestedScript(S, all_prov={"executor_id": "rogue"})
    case, ts, trials, _ = drive(open_case(), p, src, 30, r=ref(0.5))
    assert case.state != RevalState.RECOVERED
    assert not any(t.to_state == RevalState.RECOVERED for t in ts)
    failed_rows = [t for t in ts if t.trigger == RevalTrigger.GATE_FAILED]
    assert failed_rows and failed_rows[0].gates["eligible"] is False
    assert failed_rows[0].gates["failed_gates"] == ["executor_compatibility"]
    assert "gate:executor_compatibility" in failed_rows[0].detail
    assert failed_rows[0].cycle == 0 and ts[-1].trigger == RevalTrigger.CYCLES_EXHAUSTED
    assert case.state == RevalState.BUDGET_EXHAUSTED and case.cycle == 2
    assert all(t.result.counted for t in trials)  # the statistics themselves were met each time


def test_no_unattributed_timeout_can_hide_in_a_recovery() -> None:
    src = AttestedScript(lambda n: TO if n == 3 else S)  # one timeout nobody attributed
    case, ts, _, _ = drive(open_case(), rp(recovery=POLICY, max_cycles=1), src, 30, r=ref(0.5))
    assert case.state != RevalState.RECOVERED
    assert ts[-1].gates["failed_gates"] == ["verified_outcome"]


def test_a_passing_gate_proceeds_to_recovery_and_the_decision_is_reproducible() -> None:
    p = rp(recovery=POLICY)
    case, ts, trials, _ = drive(open_case(), p, AttestedScript(S), 12, r=ref(0.5))
    assert case.state == RevalState.RECOVERED and ts[-1].trigger == RevalTrigger.CONFIRMED
    stored = ts[-1].gates
    assert stored["eligible"] and stored["policy_version"] == GATES_VERSION
    assert admissibility_problems(POLICY, p.config_hash(), stored) == []
    again = evaluate_gates(
        GateContext(
            policy=POLICY, tenant_id="bench", cell_key=CELL, episode=1, cycle=0, family="timeout",
            risk="low", alt_key=ALT, ref_runtime="rt-1", trigger_at=T0,
            decision_at=datetime.fromisoformat(stored["evaluated_at"]),
            config_hash=p.config_hash(), confirm_required=p.confirm_trials,
        ),
        [t for t in trials if t.accepted],
    )  # fmt: skip
    assert again.model_dump(mode="json") == stored  # recomputed from the persisted trials alone
    assert set(stored["evidence_ids"]) == {t.key.ident() for t in trials}


def test_a_gate_leak_in_any_other_producer_is_stopped_by_the_fleet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """step_case cannot produce a recovery without a passing report; the fleet still refuses one
    from any other producer (a bug, a forged transition) - the cell stays prior-disabled."""
    forged: dict[str, dict[str, Any]] = {
        "no evaluation": {},
        "not eligible": evaluate_gates(ctx(), []).model_dump(mode="json"),
        "no policy version": payload(eligible=True, policy_version=""),
        "no evidence": payload(eligible=True, evidence_ids=[]),
        "another configuration": payload(config_hash="b" * 64),
    }
    for name, gates in forged.items():
        fleet = new_fleet(recovery=POLICY, dwell=10)
        degrade(fleet, 0, 150)
        case = fleet.cases[CELL]
        tr = RevalTransition(
            cell_key=CELL, episode=case.episode, cycle=0, seq=case.seq + 1,
            from_state=RevalState.RECOVERY_CANDIDATE, to_state=RevalState.RECOVERED,
            trigger=RevalTrigger.CONFIRMED, reason=case.reason, strategy=fleet.params.strategy,
            anchor="a", params_hash=fleet.params.config_hash(), gates=gates,
        )  # fmt: skip
        out = StepOutput(replace(case, state=RevalState.RECOVERED), [tr], [], 0, tr)
        monkeypatch.setattr(reval, "step_case", lambda *_a, _o=out, **_k: _o)
        with pytest.raises(RecoveryGateViolation):
            fleet.step(AttestedScript(S), "a", T0 + hours(500))
        assert fleet.monitor.states[CELL].locked, name  # still prior-disabled
        assert RevalState.RECOVERED not in {t.to_state for t in fleet.transitions}


# ---- the executor preflight ----------------------------------------------------------------------
def facts(**over: Any) -> ExecutorFacts:
    return AttestedScript(S, facts_over=over).facts(ref(0.5), T0)


def test_an_attested_executor_passes_the_preflight_and_each_gap_is_reported() -> None:
    assert preflight(POLICY, "rt-1", facts()) == []
    for over, gate in (
        ({"executor_id": None}, "executor_compatibility"),
        ({"executor_id": "rogue"}, "executor_compatibility"),
        ({"model_id": None}, "runtime_compatibility"),
        ({"runtime_version": "rt-9"}, "runtime_compatibility"),
        ({"tool_versions": None}, "tool_compatibility"),
        ({"verifier_id": None}, "verifier_integrity"),
        ({"verifier_independent": False}, "verifier_integrity"),
    ):
        found = preflight(POLICY, "rt-1", facts(**over))
        assert found and all(x.startswith(f"gate:{gate}") for x in found), over


def test_an_unattested_executor_is_ineligible_before_any_token_is_spent() -> None:
    for over in ({"executor_id": None}, {"infrastructure_available": False}):
        src = AttestedScript(S, facts_over=over)
        case, ts, trials, spent = drive(open_case(), rp(recovery=POLICY), src, 3, r=ref(0.5))
        assert triggers(ts) == ["INELIGIBLE"] and trials == [] and spent == 0
        assert case.state == RevalState.QUARANTINED and not case.inert  # transient: it can retry
        assert any("gate:" in d or "infrastructure" in d for d in ts[0].detail)


def test_the_runtime_must_match_the_reference_or_be_explicitly_validated() -> None:
    def runtime_set(version: str) -> list[RevalTrial]:
        return [*[trial(i, runtime_version=version) for i in range(3)],
                trial(0, TrialStage.CONFIRMATION, runtime_version=version)]  # fmt: skip

    unlisted = POLICY.model_copy(update={"validated_runtimes": ()})
    assert evaluate_gates(ctx(policy=unlisted), good_set()).eligible  # the reference runtime: match
    assert not evaluate_gates(ctx(policy=unlisted), runtime_set("rt-2")).eligible  # not validated
    listed = POLICY.model_copy(update={"validated_runtimes": ("rt-2",)})
    assert evaluate_gates(ctx(policy=listed), runtime_set("rt-2")).eligible  # validated: structural
    assert not evaluate_gates(ctx(policy=listed), runtime_set("rt-3")).eligible
    # a reference that never attested its runtime cannot be matched: only validation can help
    assert not evaluate_gates(ctx(policy=unlisted, ref_runtime=None), good_set()).eligible
    assert evaluate_gates(ctx(ref_runtime=None), good_set()).eligible  # rt-1 is in POLICY's list


def test_the_verifier_must_not_be_the_model_either() -> None:
    pol = POLICY.model_copy(
        update={"accepted_verifiers": {"verifier-x": ("v-1",), "model-a": ("v-1",)}}
    )
    report = evaluate_gates(ctx(policy=pol), good_set(trial(0, prov={"verifier_id": "model-a"})))
    assert failed(report) == {"verifier_integrity"}
    assert "independent of the executor or model" in " ".join(report.results[6].reasons)
