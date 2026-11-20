from dataclasses import dataclass, field
from typing import Protocol

from experience_engine.domain.decision import ControllerDecision, Strategy
from experience_engine.domain.task import TaskSpec
from experience_engine.execution.matcher import EligibleCapability, MatchResult

FIXED_POLICY_VERSION = "fixed-general-planning/1"
RULE_CAPABILITY_POLICY_VERSION = "rule-capability/1"
SELECTED_CAPABILITY_KEY = "selected_capability"


@dataclass(frozen=True)
class ControllerInput:
    task: TaskSpec
    run_id: str
    decision_id: str
    eligible_capabilities: list[str] = field(default_factory=list)
    match: MatchResult | None = None


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


class RuleCapabilityPolicy:
    """Deterministic capability-aware controller (no learned policy before M6).

    Only capabilities the matcher found eligible are candidates; similarity merely orders them.
    General planning is always an eligible fallback. Choosing among eligible capabilities is
    deterministic (highest similarity, then id/version), so the selection probability is 1.0.
    """

    policy_version = RULE_CAPABILITY_POLICY_VERSION

    def decide(self, ctx: ControllerInput) -> ControllerDecision:
        match = ctx.match
        eligible: list[EligibleCapability] = list(match.eligible) if match else []
        chosen = (
            min(eligible, key=lambda e: (-e.similarity, e.definition.id, e.definition.version))
            if eligible
            else None
        )
        snapshot: dict[str, object] = {
            "task_family": ctx.task.task_family,
            "risk_level": ctx.task.risk_level,
            "n_allowed_tools": len(ctx.task.allowed_tools),
            "n_catalog": match.n_catalog if match else 0,
            "n_eligible_capabilities": len(eligible),
            "candidates": match.snapshot() if match else [],
        }
        if chosen is None:
            n_rejected = len(match.rejected) if match else 0
            return self._decision(
                ctx,
                snapshot,
                [Strategy.GENERAL_PLANNING],
                {},
                Strategy.GENERAL_PLANNING,
                f"No eligible capability ({n_rejected} rejected after the eligibility gate); "
                "general planning.",
            )
        strategy = (
            Strategy.COMPOSE_CAPABILITIES
            if chosen.definition.dependencies
            else Strategy.DIRECT_CAPABILITY
        )
        snapshot[SELECTED_CAPABILITY_KEY] = chosen.ref
        return self._decision(
            ctx,
            snapshot,
            [strategy, Strategy.GENERAL_PLANNING],
            {f"similarity:{e.ref}": round(e.similarity, 4) for e in eligible},
            strategy,
            f"{chosen.ref} passed the eligibility gate and ranked first "
            f"(similarity {chosen.similarity:.2f}); general planning remains the fallback.",
        )

    def _decision(
        self,
        ctx: ControllerInput,
        snapshot: dict[str, object],
        actions: list[Strategy],
        estimates: dict[str, float],
        selected: Strategy,
        reason: str,
    ) -> ControllerDecision:
        return ControllerDecision(
            decision_id=ctx.decision_id,
            run_id=ctx.run_id,
            tenant_id=ctx.task.tenant_id,
            policy_version=self.policy_version,
            feature_snapshot=snapshot,
            eligible_actions=actions,
            estimates=estimates,
            selected_action=selected,
            selection_probability=1.0,
            reason=reason,
        )
