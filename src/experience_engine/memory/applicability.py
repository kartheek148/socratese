"""Deterministic applicability of a memory artifact to a task (never similarity)."""

from experience_engine.capabilities.applicability import EnvironmentFacts, check_conditions
from experience_engine.domain.memory import MemoryDefinition, verify_content_hash


def memory_violations(d: MemoryDefinition, facts: EnvironmentFacts) -> list[str]:
    """Reasons the artifact must NOT be injected for this task (empty = applicable)."""
    reasons: list[str] = []
    if not verify_content_hash(d):
        return ["content hash does not match the stored body (tampered or corrupt)"]
    reasons += check_conditions(d.applicability_conditions, facts)
    for c in d.exclusion_conditions:
        if not check_conditions([c], facts):  # the exclusion condition holds
            reasons.append(f"excluded: {c.kind} holds for this task")
    return reasons
