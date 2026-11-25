"""Deterministic applicability check: may this capability even be considered for this task?

Retrieval similarity is advisory only; this check is the gate (architecture §4.3). It never
executes anything and never consults a model.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from experience_engine.domain.capability import (
    CapabilityDefinition,
    FileRolePresent,
    PathRole,
    Precondition,
    RiskLevelAtMost,
    TaskFamilyIs,
    ToolsAvailable,
)

_RISK = {"low": 0, "medium": 1, "high": 2}


@dataclass(frozen=True)
class EnvironmentFacts:
    task_family: str | None
    allowed_tools: frozenset[str]
    risk_level: str
    file_roles: frozenset[PathRole]

    @staticmethod
    def of(
        task_family: str | None,
        allowed_tools: Iterable[str],
        risk_level: str,
        roles: Iterable[PathRole],
    ) -> "EnvironmentFacts":
        return EnvironmentFacts(task_family, frozenset(allowed_tools), risk_level, frozenset(roles))


def check_preconditions(definition: CapabilityDefinition, facts: EnvironmentFacts) -> list[str]:
    """Returns the list of violated preconditions (empty = applicable)."""
    return check_conditions(definition.preconditions, facts)


def check_conditions(conditions: Iterable[Precondition], facts: EnvironmentFacts) -> list[str]:
    """Shared by capabilities and memory artifacts: the typed conditions that do NOT hold."""
    violations: list[str] = []
    for p in conditions:
        if isinstance(p, TaskFamilyIs):
            if facts.task_family != p.family:
                violations.append(f"task_family {facts.task_family!r} is not {p.family!r}")
        elif isinstance(p, ToolsAvailable):
            missing = sorted(set(p.tools) - facts.allowed_tools)
            if missing:
                violations.append(f"tools not allowed for this task: {missing}")
        elif isinstance(p, FileRolePresent):
            if p.role not in facts.file_roles:
                violations.append(f"no file with role {p.role.value!r} in the environment")
        elif isinstance(p, RiskLevelAtMost) and _RISK.get(facts.risk_level, 99) > _RISK[p.level]:
            violations.append(f"risk level {facts.risk_level!r} exceeds {p.level!r}")
    return violations
