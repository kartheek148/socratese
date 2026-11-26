"""Controller policy lifecycle and rollout evidence contracts (pure data, M6)."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from experience_engine.domain.controller import canonical_hash
from experience_engine.domain.evaluation import Verdict

Metric = float | int | str | bool | None


class _F(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class PolicyState(StrEnum):
    CANDIDATE = "CANDIDATE"
    SHADOW = "SHADOW"
    CANARY = "CANARY"
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"  # was ACTIVE, replaced by a newer version; restorable by rollback
    REJECTED = "REJECTED"
    ROLLED_BACK = "ROLLED_BACK"


S = PolicyState
ALLOWED_TRANSITIONS: dict[PolicyState, frozenset[PolicyState]] = {
    S.CANDIDATE: frozenset({S.SHADOW, S.REJECTED}),
    S.SHADOW: frozenset({S.CANARY, S.REJECTED, S.ROLLED_BACK}),
    S.CANARY: frozenset({S.ACTIVE, S.ROLLED_BACK}),
    S.ACTIVE: frozenset({S.SUPERSEDED, S.ROLLED_BACK}),
    # SUPERSEDED -> ACTIVE exists only as the restore step of a rollback (service-enforced)
    S.SUPERSEDED: frozenset({S.ACTIVE}),
    S.REJECTED: frozenset(),
    S.ROLLED_BACK: frozenset(),
}


def can_transition(src: PolicyState, dst: PolicyState) -> bool:
    return dst in ALLOWED_TRANSITIONS[src]


class Stage(StrEnum):
    OFFLINE = "OFFLINE"
    SHADOW = "SHADOW"
    CANARY = "CANARY"


# the PASS evidence a target state requires
REQUIRED_STAGE: dict[PolicyState, Stage] = {
    S.SHADOW: Stage.OFFLINE,
    S.CANARY: Stage.SHADOW,
    S.ACTIVE: Stage.CANARY,
}


class OfflineGates(_F):
    min_decisions: int = Field(default=100, ge=1)
    max_ece: float = 0.10
    max_protected_deviations: int = 0
    max_infeasible_choices: int = 0
    # predicted harm check (doubly-robust success vs logged) only where the log supports it
    harm_margin: float = 0.10
    min_coverage_for_harm_check: float = 0.5


class ShadowGates(_F):
    min_decisions: int = Field(default=100, ge=1)
    max_ece: float = 0.10
    max_protected_deviations: int = 0
    max_decision_errors: int = 0


class CanaryGates(_F):
    min_per_arm: int = Field(default=30, ge=1)
    noninferiority_margin: float = Field(default=0.10, ge=0.0, le=1.0)
    confidence: float = Field(default=0.95, gt=0.5, lt=1.0)
    canary_fraction: float = Field(default=0.2, gt=0.0, lt=1.0)
    max_protected_exposures: int = 0


class RolloutGates(_F):
    offline: OfflineGates = Field(default_factory=OfflineGates)
    shadow: ShadowGates = Field(default_factory=ShadowGates)
    canary: CanaryGates = Field(default_factory=CanaryGates)

    def gate_hash(self, stage: Stage) -> str:
        part = {
            Stage.OFFLINE: self.offline,
            Stage.SHADOW: self.shadow,
            Stage.CANARY: self.canary,
        }[stage]
        return canonical_hash({"stage": stage.value, "gates": part.model_dump(mode="json")})


class StageReport(_F):
    """Evidence for one rollout stage. Metrics are JSON scalars; reasons list every unmet gate."""

    stage: Stage
    verdict: Verdict
    policy_version: str
    definition_hash: str
    gate_hash: str
    metrics: dict[str, Metric] = Field(default_factory=dict)
    reasons: list[str] = Field(default_factory=list)
