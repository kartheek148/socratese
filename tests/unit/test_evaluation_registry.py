"""Promotion policy, rollout routing, evaluation engine and registry service (fake store)."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_capability_dsl import good

from experience_engine.capabilities.registry import (
    IllegalTransitionError,
    PromotionBlockedError,
    RegistryError,
    RegistryService,
)
from experience_engine.capabilities.rollout import Route, bucket, route
from experience_engine.domain.capability import (
    CapabilityDefinition,
    CapabilityState,
    can_transition,
)
from experience_engine.domain.evaluation import (
    Arm,
    ArmMetrics,
    EvaluationReport,
    LifecycleCost,
    PromotionPolicy,
    Suite,
    Verdict,
    default_policies,
)
from experience_engine.domain.stats import wilson_interval
from experience_engine.evaluation.policy import decide_verdict, load_policies
from experience_engine.ports.registry import (
    EvaluationSummary,
    ObservationStats,
    Pointer,
    StaleStateError,
    VersionRecord,
)
from support import FakeClock

S = CapabilityState


def metrics(n: int, ok: int, tokens: int = 1000) -> ArmMetrics:
    lo, hi = wilson_interval(ok, n)
    return ArmMetrics(
        n=n,
        verified=ok,
        rate=ok / n if n else 0.0,
        ci_low=lo,
        ci_high=hi,
        tokens_total=tokens,
        tool_calls_total=n,
        latency_mean_ms=1.0,
        tokens_per_success=tokens / ok if ok else None,
        tool_calls_per_success=1.0 if ok else None,
    )


def suites(
    src=(6, 6),
    held=(4, 4),
    transfer=(4, 4),
    neg=(6, 6),
    base=(4, 4),
    cap_tokens=400,
    base_tokens=1000,
):  # type: ignore[no-untyped-def]
    c, a = Arm.CAPABILITY.value, Arm.BASELINE.value
    return {
        Suite.SOURCE_REPLAY.value: {c: metrics(*src, tokens=cap_tokens)},
        Suite.HELD_OUT.value: {
            c: metrics(*held, tokens=cap_tokens),
            a: metrics(*base, tokens=base_tokens),
        },
        Suite.TRANSFER.value: {
            c: metrics(*transfer, tokens=cap_tokens),
            a: metrics(*base, tokens=base_tokens),
        },
        Suite.NEGATIVE.value: {c: metrics(*neg)},
    }


LOW = default_policies()["low"]
MEDIUM = default_policies()["medium"]
HIGH = default_policies()["high"]


# ---- verdict --------------------------------------------------------------------------------
def test_pass_requires_all_gates() -> None:
    v, reasons = decide_verdict(LOW, suites(), false_applicability_rate=0.0, regressions=0)
    assert v == Verdict.PASS, reasons


def test_source_replay_alone_never_passes() -> None:
    only_source = {Suite.SOURCE_REPLAY.value: {Arm.CAPABILITY.value: metrics(10, 10)}}
    v, reasons = decide_verdict(LOW, only_source, false_applicability_rate=None, regressions=0)
    assert v == Verdict.INCONCLUSIVE
    assert any("held-out" in r for r in reasons) and any("transfer" in r for r in reasons)


def test_small_samples_are_inconclusive_not_pass() -> None:
    v, reasons = decide_verdict(
        LOW, suites(held=(2, 2)), false_applicability_rate=0.0, regressions=0
    )
    assert v == Verdict.INCONCLUSIVE and "held-out suite has 2 tasks" in reasons[0]
    # the same evidence under the high-risk policy needs much more data
    v2, _ = decide_verdict(HIGH, suites(), false_applicability_rate=0.0, regressions=0)
    assert v2 == Verdict.INCONCLUSIVE


@pytest.mark.parametrize(
    ("kwargs", "needle"),
    [
        (dict(src=(6, 3)), "source replay"),
        (dict(held=(4, 2)), "held-out success"),
        (dict(transfer=(4, 1)), "transfer success"),
    ],
)
def test_correctness_gates_fail(kwargs, needle) -> None:  # type: ignore[no-untyped-def]
    v, reasons = decide_verdict(
        MEDIUM, suites(**kwargs), false_applicability_rate=0.0, regressions=0
    )
    assert v == Verdict.FAIL and any(needle in r for r in reasons)


def test_false_applicability_and_regressions_fail() -> None:
    v, reasons = decide_verdict(LOW, suites(), false_applicability_rate=0.2, regressions=0)
    assert v == Verdict.FAIL and any("false applicability" in r for r in reasons)
    v, reasons = decide_verdict(MEDIUM, suites(), false_applicability_rate=0.0, regressions=1)
    assert v == Verdict.FAIL and any("regressions" in r for r in reasons)
    v, _ = decide_verdict(LOW, suites(), false_applicability_rate=0.0, regressions=1)
    assert v == Verdict.PASS  # low risk tolerates one


def test_lower_cost_cannot_buy_back_correctness() -> None:
    cheap_but_wrong = suites(held=(4, 1), transfer=(4, 1), cap_tokens=10)
    v, _ = decide_verdict(MEDIUM, cheap_but_wrong, false_applicability_rate=0.0, regressions=0)
    assert v == Verdict.FAIL


def test_token_ratio_gate_applies_only_when_configured() -> None:
    expensive = suites(cap_tokens=5000, base_tokens=1000)
    v, reasons = decide_verdict(MEDIUM, expensive, false_applicability_rate=0.0, regressions=0)
    assert v == Verdict.FAIL and any("tokens per success" in r for r in reasons)
    v, _ = decide_verdict(LOW, expensive, false_applicability_rate=0.0, regressions=0)
    assert v == Verdict.PASS  # low policy does not gate cost


def test_wilson_lower_bound_gate() -> None:
    strict = (
        replace(MEDIUM, min_success_lower_bound=0.9)
        if False
        else MEDIUM.model_copy(update={"min_success_lower_bound": 0.9})
    )
    v, reasons = decide_verdict(strict, suites(), false_applicability_rate=0.0, regressions=0)
    assert v == Verdict.FAIL and any("lower bound" in r for r in reasons)


def test_policy_hash_is_stable_and_sensitive() -> None:
    assert LOW.policy_hash() == default_policies()["low"].policy_hash()
    assert (
        LOW.policy_hash() != LOW.model_copy(update={"min_heldout_success_rate": 0.8}).policy_hash()
    )


def test_policy_file_must_define_every_risk_class(tmp_path: Path) -> None:
    pols = default_policies()
    path = tmp_path / "p.json"
    path.write_text(json.dumps({k: v.model_dump(mode="json") for k, v in pols.items()}))
    assert set(load_policies(path)) == {"low", "medium", "high"}
    path.write_text(json.dumps({"low": pols["low"].model_dump(mode="json")}))
    with pytest.raises(ValueError, match="missing"):
        load_policies(path)
    mis = {
        "low": pols["medium"].model_dump(mode="json"),
        "medium": pols["medium"].model_dump(mode="json"),
        "high": pols["high"].model_dump(mode="json"),
    }
    path.write_text(json.dumps(mis))
    with pytest.raises(ValueError, match="filed under"):
        load_policies(path)
    assert set(load_policies(None)) == {"low", "medium", "high"}


# ---- rollout routing ------------------------------------------------------------------------
def test_routing_is_deterministic_and_respects_state() -> None:
    assert route(S.ACTIVE, 0.1, "c", "t") == Route.SERVE
    assert route(S.SHADOW, 0.1, "c", "t") == Route.SHADOW
    for st in (S.CANDIDATE, S.VALIDATING, S.APPROVED, S.REJECTED, S.ROLLED_BACK, S.DEPRECATED):
        assert route(st, 1.0, "c", "t") == Route.SKIP  # never executable
    assert bucket("c", "t") == bucket("c", "t") and 0 <= bucket("c", "t") < 1
    served = sum(route(S.CANARY, 0.2, "cap", f"task-{i}") == Route.SERVE for i in range(2000))
    assert 300 < served < 500  # ~20%
    assert (
        route(S.CANARY, 1.0, "cap", "x") == Route.SERVE
        and route(S.CANARY, 0.0001, "cap", "x") == Route.SKIP
    )


def test_transition_table_matches_database_rules() -> None:
    assert can_transition(S.DEPRECATED, S.ACTIVE)  # restore on rollback only (service enforced)
    assert not can_transition(S.REJECTED, S.CANDIDATE) and not can_transition(
        S.ROLLED_BACK, S.ACTIVE
    )


# ---- registry service with an in-memory store -----------------------------------------------
def definition(risk: str | None = "low") -> CapabilityDefinition:
    raw = good()
    if risk is not None:
        raw["preconditions"].append({"kind": "risk_level_at_most", "level": risk})  # type: ignore[attr-defined]
    return CapabilityDefinition.model_validate(raw)


def test_capability_without_a_declared_risk_ceiling_gets_the_strictest_policy() -> None:
    store = FakeRegistryStore()
    svc = service(store)
    rec_default = VersionRecord("bench", ID, 1, S.CANDIDATE, definition(risk=None))
    assert svc.risk_class(rec_default) == "high"
    assert svc.policy_for(rec_default).name == "default-high"
    rec_low = VersionRecord("bench", ID, 1, S.CANDIDATE, definition("low"))
    assert svc.policy_for(rec_low).name == "default-low"


class FakeRegistryStore:
    def __init__(self, state: S = S.CANDIDATE, versions: tuple[int, ...] = (1,)) -> None:
        self.states = {v: state for v in versions}
        self.pointer = Pointer()
        self.evaluations: list[tuple[EvaluationReport, str]] = []
        self.stats: dict[tuple[int, str], ObservationStats] = {}
        self.transitions: list[tuple[int, S, S, str]] = []
        self.obs: list[tuple[int, str, bool]] = []
        self.artifact_refs: list[object] = []

    def get_version(self, tenant_id, capability_id, version):  # type: ignore[no-untyped-def]
        if version not in self.states:
            return None
        return VersionRecord(tenant_id, capability_id, version, self.states[version], definition())

    def get_pointer(self, tenant_id, capability_id) -> Pointer:  # type: ignore[no-untyped-def]
        return self.pointer

    def apply_transitions(self, *, transitions, pointer, actor, reason, **kw):  # type: ignore[no-untyped-def]
        staged = dict(self.states)
        for tr in transitions:
            if staged.get(tr.version) != tr.expected_from:
                raise StaleStateError("stale")
            staged[tr.version] = tr.to_state
        self.states = staged
        if pointer is not None:
            self.pointer = pointer
        for tr in transitions:
            self.transitions.append((tr.version, tr.expected_from, tr.to_state, actor))

    def record_evaluation(self, report, report_sha256, now):  # type: ignore[no-untyped-def]
        self.evaluations.append((report, report_sha256))
        return f"eval-{len(self.evaluations)}"

    def register_artifact(self, tenant_id, ref, uri):  # type: ignore[no-untyped-def]
        self.artifact_refs.append(ref)

    def latest_evaluation(self, tenant_id, capability_id, version):  # type: ignore[no-untyped-def]
        if not self.evaluations:
            return None
        rep = self.evaluations[-1][0]
        return EvaluationSummary(
            f"eval-{len(self.evaluations)}", rep.verdict, rep.policy_hash, rep.risk_class
        )

    def record_observation(self, **kw):  # type: ignore[no-untyped-def]
        self.obs.append((kw["version"], kw["mode"], kw["capability_verified"]))

    def observation_stats(self, tenant_id, capability_id, version, mode):  # type: ignore[no-untyped-def]
        return self.stats.get((version, mode), ObservationStats(0, 0, 0, 0))


def report(verdict: Verdict, version: int = 1, policy: PromotionPolicy = LOW) -> EvaluationReport:
    d = definition()
    s = suites()
    return EvaluationReport(
        capability_id=d.id,
        version=version,
        tenant_id="bench",
        risk_class="low",
        policy_name=policy.name,
        policy_hash=policy.policy_hash(),
        policy=policy,
        verdict=verdict,
        reasons=["r"],
        suites=s,
        false_applicability_rate=0.0,
        regressions=0,
        success_lower_bound=0.7,
        lifecycle=LifecycleCost(
            learning_tokens=0,
            learning_wall_ms=1,
            evaluation_overhead_tokens=2,
            capability_exec_tokens=3,
            capability_verified=4,
            lifecycle_tokens_per_success=1.0,
            baseline_tokens_per_success=2.0,
        ),
        dataset_versions={},
        model_settings={},
        seeds=[0],
        runtime_version="r",
        verifier_version="v",
        environment_version="e",
        evaluator_version="x",
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
    )


def service(store: FakeRegistryStore, policies=None) -> RegistryService:  # type: ignore[no-untyped-def]
    return RegistryService(store, policies or default_policies(), FakeClock())  # type: ignore[arg-type]


ID = definition().id


def test_full_lifecycle_requires_evidence_at_every_gate() -> None:
    store = FakeRegistryStore()
    svc = service(store)
    svc.start_validation("bench", ID, 1, "ev")
    assert svc.complete_evaluation(report(Verdict.PASS), "ev") == S.APPROVED
    svc.start_shadow("bench", ID, 1, "ev")
    with pytest.raises(PromotionBlockedError, match="shadow observations"):
        svc.advance_to_canary("bench", ID, 1, "ev")
    store.stats[(1, "SHADOW")] = ObservationStats(
        n=10, capability_verified=9, n_paired=10, regressions=0
    )
    svc.advance_to_canary("bench", ID, 1, "ev")
    with pytest.raises(PromotionBlockedError, match="canary observations"):
        svc.activate("bench", ID, 1, "ev")
    store.stats[(1, "CANARY")] = ObservationStats(
        n=10, capability_verified=9, n_paired=10, regressions=0
    )
    svc.activate("bench", ID, 1, "ev")
    assert store.states[1] == S.ACTIVE and store.pointer == Pointer(1, None)
    assert [t[2] for t in store.transitions] == [
        S.VALIDATING,
        S.APPROVED,
        S.SHADOW,
        S.CANARY,
        S.ACTIVE,
    ]


def test_failed_and_inconclusive_evaluations() -> None:
    store = FakeRegistryStore()
    svc = service(store)
    svc.start_validation("bench", ID, 1, "ev")
    assert svc.complete_evaluation(report(Verdict.INCONCLUSIVE), "ev") == S.VALIDATING
    assert store.states[1] == S.VALIDATING
    assert svc.complete_evaluation(report(Verdict.FAIL), "ev") == S.REJECTED
    with pytest.raises(IllegalTransitionError):
        svc.start_shadow("bench", ID, 1, "ev")  # REJECTED is terminal


def test_cannot_skip_stages_or_promote_without_a_passing_evaluation() -> None:
    store = FakeRegistryStore()
    svc = service(store)
    with pytest.raises(IllegalTransitionError):
        svc.start_shadow("bench", ID, 1, "ev")  # CANDIDATE -> SHADOW
    store.states[1] = S.APPROVED  # forced state without an evaluation record
    with pytest.raises(PromotionBlockedError, match="no passing evaluation"):
        svc.start_shadow("bench", ID, 1, "ev")
    with pytest.raises(IllegalTransitionError):
        svc.activate("bench", ID, 1, "ev") if False else svc._one(  # APPROVED -> ACTIVE
            store.get_version("bench", ID, 1),
            S.ACTIVE,
            "ev",
            "x",  # type: ignore[arg-type]
        )


def test_policy_change_after_evaluation_blocks_promotion() -> None:
    store = FakeRegistryStore()
    svc = service(store)
    svc.start_validation("bench", ID, 1, "ev")
    svc.complete_evaluation(report(Verdict.PASS), "ev")
    changed = default_policies()
    changed["low"] = changed["low"].model_copy(update={"min_heldout_success_rate": 0.9})
    with pytest.raises(PromotionBlockedError, match="policy changed"):
        service(store, changed).start_shadow("bench", ID, 1, "ev")


def test_shadow_failure_rate_and_regression_gates() -> None:
    store = FakeRegistryStore(S.SHADOW)
    svc = service(store)
    store.stats[(1, "SHADOW")] = ObservationStats(
        n=10, capability_verified=5, n_paired=10, regressions=0
    )
    with pytest.raises(PromotionBlockedError, match="failure rate"):
        svc.advance_to_canary("bench", ID, 1, "ev")
    store.stats[(1, "SHADOW")] = ObservationStats(
        n=10, capability_verified=10, n_paired=10, regressions=3
    )
    with pytest.raises(PromotionBlockedError, match="regressions"):
        svc.advance_to_canary("bench", ID, 1, "ev")


def test_activation_keeps_previous_version_and_rollback_restores_it() -> None:
    store = FakeRegistryStore(S.CANARY, versions=(1, 2))
    store.states[1] = S.ACTIVE
    store.pointer = Pointer(1, None)
    svc = service(store)
    store.stats[(2, "CANARY")] = ObservationStats(
        n=10, capability_verified=10, n_paired=10, regressions=0
    )
    svc.activate("bench", ID, 2, "ev")
    assert (store.states[1], store.states[2]) == (S.DEPRECATED, S.ACTIVE)
    assert store.pointer == Pointer(2, 1)
    svc.rollback("bench", ID, 2, "ev", "regression found in production")
    assert (store.states[1], store.states[2]) == (S.ACTIVE, S.ROLLED_BACK)  # v1 is back, never lost
    assert store.pointer == Pointer(1, None)


def test_rollback_of_sole_active_version_and_of_rollout_stages() -> None:
    store = FakeRegistryStore(S.ACTIVE)
    store.pointer = Pointer(1, None)
    svc = service(store)
    svc.rollback("bench", ID, 1, "ev", "bad")
    assert store.states[1] == S.ROLLED_BACK and store.pointer == Pointer(None, None)
    canary = FakeRegistryStore(S.CANARY)
    service(canary).rollback("bench", ID, 1, "ev", "bad canary")
    assert canary.states[1] == S.ROLLED_BACK and canary.pointer == Pointer()  # pointer untouched
    with pytest.raises(IllegalTransitionError):
        service(FakeRegistryStore(S.CANDIDATE)).rollback("bench", ID, 1, "ev", "x")
    with pytest.raises(RegistryError):
        service(FakeRegistryStore()).start_validation("bench", ID, 99, "ev")


def test_observations_only_for_the_matching_rollout_stage() -> None:
    store = FakeRegistryStore(S.SHADOW)
    svc = service(store)
    rec = store.get_version("bench", ID, 1)
    assert rec is not None
    svc.record_observation(rec, "SHADOW", "t1", True, True, 10, 100)
    with pytest.raises(IllegalTransitionError):
        svc.record_observation(rec, "CANARY", "t1", True, True, 10, 100)
    assert store.obs == [(1, "SHADOW", True)]
