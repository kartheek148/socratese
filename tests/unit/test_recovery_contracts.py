"""M6.4 Phase 1 - contracts and backward compatibility: frozen parameters, the outcome classes,
provenance that never invents data, and the guarantee that policies without a recovery block keep
every serialised form and hash they had under M6.3."""

import pytest
from pydantic import ValidationError
from test_revalidation_core import F, S, U, vr

from experience_engine.controller.revalidation import classify_run_outcome
from experience_engine.domain.controller import ControllerParams
from experience_engine.domain.outcome import OutcomeStatus
from experience_engine.domain.recovery import (
    EVIDENCE_CLASSES,
    GATES_VERSION,
    OUTAGE_VERSION,
    OutcomeClass,
    RecoveryPolicy,
    TimeoutOrigin,
    TrialProvenance,
)
from experience_engine.domain.revalidation import (
    NON_EVIDENCE_VERDICTS,
    ExecutorFacts,
    RevalidationParams,
    RevalStrategy,
    TrialResult,
    TrialVerdict,
)
from recovery_support import POLICY

# computed with the M6.3 code (the commit before M6.4 Phase 1): they must never change
M63_HASHES = {
    RevalStrategy.FIXED: "132a1b81fcca23cc85b92ed0c2e57ebac19206707ff2852dabbea673cf9f6b10",
    RevalStrategy.EVIDENCE_THRESHOLD: (
        "ad5def7ec7e4780698718600b730edbe83c0a4dd04c4bb26ba88c446931f48c0"
    ),
    RevalStrategy.SEQUENTIAL: "04f3378f90c06a67f0c4792523b0e366f78827601a74833b2e59dfcfd83fbe08",
}
M63_TUNED = "5bba6bd3e0d876c275ee4af533ded0eb0e481219fd9405e021dabfdf8822485a"
M63_FACTS = {
    "pool": 0, "runtime_version": "r", "supported": True, "tenant_id": "t",
    "verification_available": True, "verifier_version": None,
}  # fmt: skip


def test_the_frozen_outage_and_gate_parameters_are_the_proposed_initial_configuration() -> None:
    p = RecoveryPolicy()
    assert p.outage_threshold == 3 and p.resume_probes == 2
    assert p.evidence_window_hours == 120.0 and p.max_suspension_hours == 500.0
    assert p.gates_version == GATES_VERSION == "m6.4-gates-v1"
    assert OUTAGE_VERSION == "m6.4-outage-v1"
    assert not (p.accepted_executors or p.accepted_models or p.accepted_tools
                or p.accepted_verifiers or p.accepted_sources or p.validated_runtimes)  # fmt: skip


@pytest.mark.parametrize(
    "bad",
    [{"outage_threshold": 0}, {"resume_probes": 0}, {"evidence_window_hours": 0.0},
     {"max_suspension_hours": 0.0}, {"surprise": 1}],
)  # fmt: skip
def test_the_policy_rejects_invalid_parameters(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        RecoveryPolicy(**bad)  # type: ignore[arg-type]


def test_the_policy_is_canonical_so_equal_policies_have_equal_hashes() -> None:
    a = RecoveryPolicy(accepted_executors=("b", "a", "a"), accepted_models={"m": ("2", "1")})
    b = RecoveryPolicy(accepted_executors=("a", "b"), accepted_models={"m": ("1", "2")})
    assert a == b
    ha = RevalidationParams(strategy=RevalStrategy.FIXED, recovery=a).config_hash()
    assert ha == RevalidationParams(strategy=RevalStrategy.FIXED, recovery=b).config_hash()


# ---- M6.3 compatibility --------------------------------------------------------------------------
@pytest.mark.parametrize("strategy", list(RevalStrategy))
def test_m63_configuration_hashes_are_unchanged(strategy: RevalStrategy) -> None:
    assert RevalidationParams(strategy=strategy).config_hash() == M63_HASHES[strategy]
    tuned = RevalidationParams(strategy=RevalStrategy.SEQUENTIAL, dwell=10, alpha=0.1)
    assert tuned.config_hash() == M63_TUNED


def test_an_unset_recovery_block_is_omitted_and_a_set_one_changes_the_hash() -> None:
    plain = RevalidationParams(strategy=RevalStrategy.FIXED)
    assert "recovery" not in plain.model_dump(mode="json")
    assert (
        "recovery"
        not in ControllerParams.model_validate(
            {"evidence": {}, "drift": {}, "revalidation": {"strategy": "FIXED"}}
        ).model_dump(mode="json")["revalidation"]
    )
    gated = RevalidationParams(strategy=RevalStrategy.FIXED, recovery=POLICY)
    assert gated.model_dump(mode="json")["recovery"]["gates_version"] == GATES_VERSION
    assert gated.config_hash() != plain.config_hash()
    other = POLICY.model_copy(update={"evidence_window_hours": 24.0})
    assert RevalidationParams(strategy=RevalStrategy.FIXED, recovery=other).config_hash() != (
        gated.config_hash()
    )  # a different policy is a different, auditable configuration
    round_trip = RevalidationParams.model_validate(gated.model_dump(mode="json"))
    assert round_trip == gated and round_trip.config_hash() == gated.config_hash()


def test_executor_facts_serialise_exactly_as_before_unless_the_new_fields_are_set() -> None:
    plain = ExecutorFacts(tenant_id="t", runtime_version="r")
    assert plain.to_json() == M63_FACTS
    attested = ExecutorFacts(tenant_id="t", executor_id="e", infrastructure_available=False,
                             tool_versions={})  # fmt: skip
    data = attested.to_json()
    assert data["executor_id"] == "e" and data["infrastructure_available"] is False
    assert data["tool_versions"] == {} and "model_id" not in data
    assert ExecutorFacts.model_validate(data) == attested  # and it round-trips
    assert ExecutorFacts.model_validate(M63_FACTS) == plain  # M6.3 rows still load


def test_an_m63_trial_row_still_validates_and_has_explicitly_missing_provenance() -> None:
    old = {
        "verdict": "SUCCESS", "tokens": 5, "task_ref": "t", "observed_at": "2026-01-01T00:00:00Z",
        "runtime_version": "r", "verifier_version": "v", "capability_ref": "c",
        "latency_ms": None, "tenant_id": "x",
    }  # fmt: skip
    res = TrialResult.model_validate(old)
    assert res.provenance == TrialProvenance() and res.timeout_origin is None
    assert all(v is None for v in res.provenance.model_dump().values())  # nothing invented


# ---- the seven outcome classes -------------------------------------------------------------------
def test_every_verdict_maps_to_one_of_the_seven_named_classes() -> None:
    expected = {
        TrialVerdict.SUCCESS: "VERIFIED_SUCCESS",
        TrialVerdict.FAILURE: "VERIFIED_FAILURE",
        TrialVerdict.TIMEOUT: "CAPABILITY_TIMEOUT",
        TrialVerdict.INVALID_OUTPUT: "INVALID_OUTPUT",
        TrialVerdict.VERIFICATION_UNAVAILABLE: "VERIFICATION_UNAVAILABLE",
        TrialVerdict.INFRASTRUCTURE_FAILURE: "INFRASTRUCTURE_FAILURE",
        TrialVerdict.EXECUTION_INCOMPLETE: "EXECUTION_INCOMPLETE",
    }
    assert set(expected) == set(TrialVerdict) and len(OutcomeClass) == 7
    for verdict, name in expected.items():
        res = TrialResult(
            verdict=verdict, tokens=1, task_ref="t", observed_at="2026-01-01T00:00:00Z"
        )  # type: ignore[arg-type]
        assert res.outcome_class.value == name
        assert res.counted == (res.outcome_class in EVIDENCE_CLASSES)
        assert res.counted != (verdict in NON_EVIDENCE_VERDICTS)
    assert {
        U,
        TrialVerdict.INFRASTRUCTURE_FAILURE,
        TrialVerdict.EXECUTION_INCOMPLETE,
    } == NON_EVIDENCE_VERDICTS


def test_only_verified_success_is_a_success_and_nothing_non_evidence_ever_is() -> None:
    for verdict in TrialVerdict:
        res = TrialResult(
            verdict=verdict, tokens=1, task_ref="t", observed_at="2026-01-01T00:00:00Z"
        )  # type: ignore[arg-type]
        assert res.success == (verdict == S)
        if verdict in NON_EVIDENCE_VERDICTS:
            assert not res.success and not res.counted


def test_the_executor_adapter_classification_takes_precedence_in_the_documented_order() -> None:
    ok = [vr(True)]
    unavailable = [vr(None)]
    assert classify_run_outcome(OutcomeStatus.VERIFIED_SUCCESS, ok) == S  # unchanged
    assert classify_run_outcome(OutcomeStatus.FAILURE, ok) == F
    assert classify_run_outcome(OutcomeStatus.UNKNOWN, unavailable) == U
    assert classify_run_outcome(OutcomeStatus.FAILURE, ok, timed_out=True) == TrialVerdict.TIMEOUT
    full = {"timed_out": True, "invalid_output": True}
    assert classify_run_outcome(OutcomeStatus.UNKNOWN, unavailable, **full) == U
    assert (
        classify_run_outcome(OutcomeStatus.UNKNOWN, unavailable, infrastructure_failed=True, **full)
        == TrialVerdict.INFRASTRUCTURE_FAILURE
    )
    assert (
        classify_run_outcome(
            OutcomeStatus.VERIFIED_SUCCESS, ok, infrastructure_failed=True, incomplete=True
        )
        == TrialVerdict.EXECUTION_INCOMPLETE
    )  # a run that never finished says nothing at all
    assert (
        classify_run_outcome(OutcomeStatus.VERIFIED_SUCCESS, ok, infrastructure_failed=True)
        == TrialVerdict.INFRASTRUCTURE_FAILURE
    )  # not a success, whatever the environment looked like


def test_provenance_records_only_what_was_attested() -> None:
    assert TrialProvenance().to_json() == {}
    p = TrialProvenance(executor_id="e", tool_versions={})
    assert p.to_json() == {
        "executor_id": "e",
        "tool_versions": {},
    }  # {} is a statement, None is not
    assert TrialProvenance.model_validate(p.to_json()) == p
    with pytest.raises(ValidationError):
        TrialProvenance(unknown_field="x")  # type: ignore[call-arg]
    assert TimeoutOrigin.CAPABILITY != TimeoutOrigin.INFRASTRUCTURE
