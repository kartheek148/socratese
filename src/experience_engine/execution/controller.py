from dataclasses import dataclass, field
from typing import Protocol

from experience_engine.domain.decision import ControllerDecision, Strategy
from experience_engine.domain.task import TaskSpec

FIXED_POLICY_VERSION = "fixed-general-planning/1"


@dataclass(frozen=True)
class ControllerInput:
    task: TaskSpec
    run_id: str
    decision_id: str
    eligible_capabilities: list[str] = field(default_factory=list)


class Controller(Protocol):
    policy_version: str

    def decide(self, ctx: ControllerInput) -> ControllerDecision: ...


class FixedPolicy:
    """Baseline controller: always plans from scratch. No capabilities exist before milestone 4."""

    policy_version = FIXED_POLICY_VERSION

    def decide(self, ctx: ControllerInput) -> ControllerDecision:
        return ControllerDecision(
            decision_id=ctx.decision_id,
            run_id=ctx.run_id,
            tenant_id=ctx.task.tenant_id,
            policy_version=self.policy_version,
            feature_snapshot={
                "task_family": ctx.task.task_family,
                "risk_level": ctx.task.risk_level,
                "n_allowed_tools": len(ctx.task.allowed_tools),
                "n_eligible_capabilities": len(ctx.eligible_capabilities),
            },
            eligible_actions=[Strategy.GENERAL_PLANNING],
            estimates={},
            selected_action=Strategy.GENERAL_PLANNING,
            selection_probability=1.0,
            reason="Baseline: no approved capabilities; general planning is the only eligible "
            "action.",
        )
