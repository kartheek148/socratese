"""Recovery evidence contracts (pure data, M6.4 Phase 1).

Two ideas, both fail-closed. (1) A trial is *attempted* (it spends budget) and only some attempts
are *evidence* (they enter the success statistics); an unavailable verifier, an infrastructure
failure or an interrupted run is never a capability observation. (2) Evidence can contribute to a
recovery only when every gate of a versioned gate policy passes over its recorded provenance;
absent provenance is a failed gate, never a permissive default. Design and frozen parameters:
docs/research/m6_4_foundation.md.
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

GATES_VERSION = "m6.4-gates-v1"
OUTAGE_VERSION = "m6.4-outage-v1"


class _F(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class TimeoutOrigin(StrEnum):
    """Who is responsible for a timeout, stated by the executor adapter (never inferred later)."""

    CAPABILITY = "CAPABILITY"  # the capability ran out of steps, tokens or (healthy-path) time
    INFRASTRUCTURE = "INFRASTRUCTURE"  # the provider, executor or network was down or too slow


class VerificationStatus(StrEnum):
    VERIFIED = "VERIFIED"  # the independent verifier evaluated the environment and decided
    UNAVAILABLE = "UNAVAILABLE"  # the verifier could not evaluate (down, or an unevaluable check)
    NOT_COMPLETED = "NOT_COMPLETED"  # the run never reached verification


class OutcomeClass(StrEnum):
    """The seven outcome classes of M6.4 Phase 1 (a derived view of `TrialVerdict`, which stays
    the stored, backward compatible form: SUCCESS = VERIFIED_SUCCESS, FAILURE = VERIFIED_FAILURE,
    TIMEOUT = CAPABILITY_TIMEOUT)."""

    VERIFIED_SUCCESS = "VERIFIED_SUCCESS"
    VERIFIED_FAILURE = "VERIFIED_FAILURE"
    CAPABILITY_TIMEOUT = "CAPABILITY_TIMEOUT"
    INVALID_OUTPUT = "INVALID_OUTPUT"
    VERIFICATION_UNAVAILABLE = "VERIFICATION_UNAVAILABLE"
    INFRASTRUCTURE_FAILURE = "INFRASTRUCTURE_FAILURE"
    EXECUTION_INCOMPLETE = "EXECUTION_INCOMPLETE"


EVIDENCE_CLASSES = frozenset(
    {
        OutcomeClass.VERIFIED_SUCCESS,
        OutcomeClass.VERIFIED_FAILURE,
        OutcomeClass.CAPABILITY_TIMEOUT,
        OutcomeClass.INVALID_OUTPUT,
    }
)


class TrialProvenance(_F):
    """What the executor attests about one trial. `None` always means *unavailable* (the adapter
    did not attest it); no field is ever filled with a permissive default, and the gates treat an
    unavailable field as a failure. `tool_versions == {}` is an explicit statement that no tool
    was used, which is different from `None`. Capability, tenant, runtime, verifier version,
    outcome, timestamp and trial identity are already first-class fields of the trial row."""

    executor_id: str | None = None
    model_id: str | None = None
    model_version: str | None = None
    tool_versions: dict[str, str] | None = None
    verifier_id: str | None = None
    verifier_independent: bool | None = None  # attested by the adapter, never by the model
    evidence_source: str | None = None
    execution_id: str | None = None
    pool_id: str | None = None  # the revalidation task pool the task was drawn from
    task_family: str | None = None
    risk_level: str | None = None
    verification_status: VerificationStatus | None = None

    def to_json(self) -> dict[str, object]:
        return self.model_dump(mode="json", exclude_none=True)


class RecoveryPolicy(_F):
    """Frozen M6.4 Phase 1 policy: outage semantics and the gate allow-lists. Part of the
    revalidation parameters (so of the policy hash). The outage values are experimental, not
    optimal. Empty allow-lists accept nothing: a policy that does not name its executors, models,
    tools, verifiers and sources can never recover a cell."""

    gates_version: str = GATES_VERSION
    outage_threshold: int = Field(default=3, ge=1)  # consecutive unavailable observations
    resume_probes: int = Field(default=2, ge=1)  # consecutive healthy probes to resume
    evidence_window_hours: float = Field(default=120.0, gt=0.0)
    max_suspension_hours: float = Field(default=500.0, gt=0.0)  # cumulative, per case
    accepted_executors: tuple[str, ...] = ()
    accepted_models: dict[str, tuple[str, ...]] = Field(default_factory=dict)  # id -> versions
    validated_runtimes: tuple[str, ...] = ()  # runtimes explicitly validated besides the reference
    accepted_tools: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    accepted_verifiers: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    accepted_sources: tuple[str, ...] = ()

    @field_validator("accepted_executors", "validated_runtimes", "accepted_sources")
    @classmethod
    def _canonical_tuple(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(set(v)))

    @field_validator("accepted_models", "accepted_tools", "accepted_verifiers")
    @classmethod
    def _canonical_map(cls, v: dict[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
        return {k: tuple(sorted(set(vs))) for k, vs in sorted(v.items())}


class GateId(StrEnum):
    EVIDENCE_PRESENCE = "evidence_presence"  # gate 1
    CAPABILITY_IDENTITY = "capability_identity"  # 2
    CONTEXT_ISOLATION = "context_isolation"  # 3
    EXECUTOR_COMPATIBILITY = "executor_compatibility"  # 4
    RUNTIME_COMPATIBILITY = "runtime_compatibility"  # 5 (model and runtime)
    TOOL_COMPATIBILITY = "tool_compatibility"  # 6
    VERIFIER_INTEGRITY = "verifier_integrity"  # 7
    EVIDENCE_FRESHNESS = "evidence_freshness"  # 8
    VERIFIED_OUTCOME = "verified_outcome"  # 9
    LINEAGE_INTEGRITY = "lineage_integrity"  # 10
    POLICY_INTEGRITY = "policy_integrity"  # 11


class GateResult(_F):
    gate: GateId
    passed: bool
    checked: int = Field(ge=0)  # evidence items the gate looked at
    failed_evidence: list[str] = Field(default_factory=list)  # trial identities
    reasons: list[str] = Field(default_factory=list)  # distinct, sorted, bounded

    @model_validator(mode="after")
    def _consistent(self) -> "GateResult":
        if self.passed != (not self.reasons and not self.failed_evidence):
            raise ValueError("a gate passes exactly when it records no failure")
        return self


class GateReport(_F):
    """The persisted decision of one gate evaluation. `eligible` is *derived*: a payload that
    asserts it without all eleven passing results, an evidence reference and a policy version is
    structurally invalid, so a populated payload is never proof that the gates passed."""

    eligible: bool
    policy_version: str
    failed_gates: list[str]
    passed_gates: list[str]
    evidence_ids: list[str]
    results: list[GateResult]
    evaluated_at: datetime | None
    config_hash: str
    tenant_id: str
    cell_key: str
    episode: int = Field(ge=0)
    cycle: int = Field(ge=0)

    @model_validator(mode="after")
    def _derived(self) -> "GateReport":
        failed = [r.gate.value for r in self.results if not r.passed]
        passed = [r.gate.value for r in self.results if r.passed]
        if self.failed_gates != failed or self.passed_gates != passed:
            raise ValueError("failed/passed gates do not match the gate results")
        complete = {r.gate for r in self.results} == set(GateId) and len(self.results) == len(
            GateId
        )
        derived = complete and not failed and bool(self.evidence_ids) and bool(self.policy_version)
        if self.eligible != derived:
            raise ValueError("eligible does not follow from the gate results")
        return self
