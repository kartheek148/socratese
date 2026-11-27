"""Runtime rollout wiring: canary routing, shadow attachment, controller built from the registry."""

from dataclasses import dataclass

from experience_engine.controller.drift_service import DriftService
from experience_engine.controller.gates import canary_assignment
from experience_engine.controller.policy import AdaptiveController
from experience_engine.domain.controller import ControllerPolicyDefinition
from experience_engine.domain.decision import ControllerDecision
from experience_engine.domain.rollout import PolicyState
from experience_engine.execution.controller import Controller, ControllerInput
from experience_engine.ports.controller_store import ControllerPolicyStore


class CanaryRouter:
    """Sends a stable share of eligible tasks to the candidate; protected tasks never reach it.

    Assignment is a hash of the task id, so a task always lands in the same arm and the split is
    reproducible. The router adds no policy of its own: every decision is the delegate's.
    """

    def __init__(
        self,
        control: Controller,
        candidate: AdaptiveController,
        fraction: float,
        salt: str = "canary",
    ):
        if not 0.0 < fraction < 1.0:
            raise ValueError("canary fraction must be in (0, 1)")
        self._control = control
        self._candidate = candidate
        self._fraction = fraction
        self._salt = salt
        self.policy_version = control.policy_version

    def routes_to_candidate(self, ctx: ControllerInput) -> bool:
        learned = self._candidate.definition.params.learned_risk_levels
        return ctx.task.risk_level in learned and canary_assignment(
            ctx.task.task_id, self._fraction, self._salt
        )

    def decide(self, ctx: ControllerInput) -> ControllerDecision:
        return (self._candidate if self.routes_to_candidate(ctx) else self._control).decide(ctx)


@dataclass(frozen=True)
class RolloutSetup:
    controller: Controller
    shadow: Controller | None
    active_version: str | None
    canary_version: str | None
    shadow_version: str | None


def build_rollout(
    store: ControllerPolicyStore,
    tenant_id: str,
    policy_id: str,
    baseline: Controller,
    canary_fraction: float = 0.2,
    drift: DriftService | None = None,
) -> RolloutSetup:
    """The production controller for a tenant: ACTIVE policy (else the rule baseline), with the
    CANARY candidate split in and the SHADOW candidate attached. States come from the registry,
    never from a mutable config, so a rollback takes effect on the next build."""
    records = {r.state: r for r in store.list_versions(tenant_id, policy_id)}

    def build(definition: ControllerPolicyDefinition) -> AdaptiveController:
        # M6.2: the monitor is synced (and its transitions persisted) before its state can act
        overlay = drift.sync(definition).overlay if drift is not None else None
        return AdaptiveController(definition, overlay)

    active_rec = records.get(PolicyState.ACTIVE)
    base: Controller = build(active_rec.definition) if active_rec else baseline
    canary_rec = records.get(PolicyState.CANARY)
    shadow_rec = records.get(PolicyState.SHADOW)
    controller: Controller = (
        CanaryRouter(base, build(canary_rec.definition), canary_fraction) if canary_rec else base
    )
    return RolloutSetup(
        controller=controller,
        shadow=AdaptiveController(shadow_rec.definition) if shadow_rec else None,
        active_version=active_rec.definition.policy_version if active_rec else None,
        canary_version=canary_rec.definition.policy_version if canary_rec else None,
        shadow_version=shadow_rec.definition.policy_version if shadow_rec else None,
    )
