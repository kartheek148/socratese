"""Feasibility layer: the rule baseline and the feasible action set. No learning, no estimates.

Eligibility itself (permissions, tenant, preconditions, budgets, ACTIVE state) is decided by the
matcher before the controller runs; this layer only turns its result into the action set and the
rule baseline's default. It is deliberately the same ordering `RuleCapabilityPolicy` uses, so the
rule baseline is reproduced exactly by `default_key`.
"""

from dataclasses import dataclass

from experience_engine.domain.controller import (
    PLANNING_KEY,
    CandidateFeature,
    LearnedRisk,
    action_key,
)
from experience_engine.domain.decision import Strategy
from experience_engine.execution.matcher import EligibleCapability, MatchResult


@dataclass(frozen=True)
class FeasibleSet:
    actions: list[CandidateFeature]  # planning first, then eligible capabilities by rule rank
    default_key: str


def capability_strategy(e: EligibleCapability) -> Strategy:
    return (
        Strategy.COMPOSE_CAPABILITIES if e.definition.dependencies else Strategy.DIRECT_CAPABILITY
    )


def feasible_actions(match: MatchResult | None) -> FeasibleSet:
    planning = CandidateFeature(key=PLANNING_KEY, strategy=Strategy.GENERAL_PLANNING)
    eligible = sorted(
        match.eligible if match else [],
        key=lambda e: (-e.similarity, e.definition.id, e.definition.version),
    )
    caps = [
        CandidateFeature(
            key=action_key(capability_strategy(e), e.ref),
            strategy=capability_strategy(e),
            ref=e.ref,
            similarity=round(e.similarity, 4),
        )
        for e in eligible
    ]
    default = caps[0].key if caps else PLANNING_KEY
    return FeasibleSet(actions=[planning, *caps], default_key=default)


def is_protected(risk: str, learned_risk_levels: list[LearnedRisk]) -> bool:
    """Risk classes the learned policy may not influence at all."""
    return risk not in learned_risk_levels
