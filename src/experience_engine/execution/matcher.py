"""Runtime capability matching: advisory retrieval, then deterministic eligibility.

Retrieval only *shortlists and ranks*. It can be lexical, structured or (later) embedding-based
and it can be wrong. Whether a capability may run is decided afterwards, from the task and the
environment, by `check_eligibility`, which never looks at similarity. A capability with a
similarity of 0.99 is rejected exactly like one with 0.01 if a precondition fails (architecture
§4.3: "retrieval is advisory; capability eligibility and permissions are checked separately").
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from experience_engine.capabilities.applicability import EnvironmentFacts, check_preconditions
from experience_engine.capabilities.validator import (
    CapabilityValidationError,
    validate_definition,
)
from experience_engine.domain.capability import CapabilityDefinition, TaskFamilyIs, ToolCall
from experience_engine.domain.task import TaskSpec
from experience_engine.ports.experience import ExecutableCapabilityCatalog

_WORD = re.compile(r"[a-z0-9]+")
MAX_DEPENDENCY_DEPTH = 3


@dataclass(frozen=True)
class ScoredCandidate:
    definition: CapabilityDefinition
    similarity: float


class CapabilityRetriever(Protocol):
    def rank(
        self, task: TaskSpec, candidates: Sequence[CapabilityDefinition]
    ) -> list[ScoredCandidate]:
        """Most promising first. Advisory only."""
        ...


def _tokens(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


class LexicalRetriever:
    """Jaccard overlap of the task objective/family with the capability objective/family."""

    def rank(
        self, task: TaskSpec, candidates: Sequence[CapabilityDefinition]
    ) -> list[ScoredCandidate]:
        query = _tokens(task.objective) | _tokens(task.task_family or "")
        scored = []
        for d in candidates:
            family = next((p.family for p in d.preconditions if isinstance(p, TaskFamilyIs)), "")
            doc = _tokens(d.objective) | _tokens(family)
            union = query | doc
            scored.append(ScoredCandidate(d, len(query & doc) / len(union) if union else 0.0))
        return sorted(scored, key=lambda s: (-s.similarity, s.definition.id, s.definition.version))


@dataclass(frozen=True)
class MatchPolicy:
    max_candidates: int = 5
    # retrieval cut-off: below it a capability is not even considered. It is not an eligibility
    # criterion; with the default of 0 retrieval never hides an otherwise eligible capability.
    min_similarity: float = 0.0


@dataclass(frozen=True)
class EligibleCapability:
    definition: CapabilityDefinition
    similarity: float

    @property
    def ref(self) -> str:
        return f"{self.definition.id}@{self.definition.version}"


@dataclass(frozen=True)
class Rejected:
    capability_id: str
    version: int
    similarity: float
    reasons: list[str]


@dataclass
class MatchResult:
    n_catalog: int = 0
    eligible: list[EligibleCapability] = field(default_factory=list)
    rejected: list[Rejected] = field(default_factory=list)

    def snapshot(self, limit: int = 10) -> list[dict[str, object]]:
        """Bounded, JSON-safe record of what was considered (for the decision log)."""
        rows: list[dict[str, object]] = [
            {
                "capability": e.ref,
                "similarity": round(e.similarity, 4),
                "eligible": True,
                "reasons": [],
                "strategy": "COMPOSE_CAPABILITIES"
                if e.definition.dependencies
                else "DIRECT_CAPABILITY",
            }
            for e in self.eligible
        ]
        rows += [
            {
                "capability": f"{r.capability_id}@{r.version}",
                "similarity": round(r.similarity, 4),
                "eligible": False,
                "reasons": [x[:120] for x in r.reasons[:3]],
            }
            for r in self.rejected
        ]
        return rows[:limit]


def check_eligibility(
    definition: CapabilityDefinition,
    task: TaskSpec,
    facts: EnvironmentFacts,
    catalog: ExecutableCapabilityCatalog,
) -> list[str]:
    """Deterministic gate. Returns the reasons a capability may NOT run (empty = eligible)."""
    reasons: list[str] = []
    try:
        validate_definition(definition, known_dependencies=definition.dependencies)
    except CapabilityValidationError as exc:
        return [f"definition failed static validation: {str(exc)[:100]}"]
    reasons += check_preconditions(definition, facts)
    denied = sorted(set(definition.permissions) - set(task.allowed_tools))
    if denied:
        reasons.append(f"permissions not granted by the task: {denied}")
    b = definition.budgets
    if b.max_steps > task.max_steps:
        reasons.append(f"capability may need {b.max_steps} steps; task allows {task.max_steps}")
    if b.max_tokens > task.max_tokens:
        reasons.append(f"capability may use {b.max_tokens} tokens; task allows {task.max_tokens}")
    if b.timeout_ms > task.deadline_ms:
        reasons.append(f"capability timeout {b.timeout_ms} ms exceeds task deadline")
    n_tool_steps = sum(isinstance(s, ToolCall) for s in definition.steps)
    if n_tool_steps > task.max_tool_calls:
        reasons.append(
            f"capability needs {n_tool_steps} tool calls; task allows {task.max_tool_calls}"
        )
    reasons += _dependency_problems(definition, task, catalog, (definition.id,), 1)
    return reasons


def _dependency_problems(
    definition: CapabilityDefinition,
    task: TaskSpec,
    catalog: ExecutableCapabilityCatalog,
    stack: tuple[str, ...],
    depth: int,
) -> list[str]:
    problems: list[str] = []
    if definition.dependencies and depth > MAX_DEPENDENCY_DEPTH:
        return ["dependency chain too deep"]
    for dep in definition.dependencies:
        dep_id, _, ver = dep.partition("@")
        if dep_id in stack:
            problems.append(f"dependency cycle through {dep_id}")
            continue
        child = catalog.get_executable(task.tenant_id, dep_id, int(ver)) if ver.isdigit() else None
        if child is None:
            problems.append(f"dependency {dep} is not an executable version")
            continue
        if not set(child.permissions) <= set(definition.permissions):
            problems.append(f"dependency {dep} needs tools the parent does not declare")
        problems += _dependency_problems(child, task, catalog, (*stack, dep_id), depth + 1)
    return problems


class CapabilityMatcher:
    def __init__(
        self,
        catalog: ExecutableCapabilityCatalog,
        retriever: CapabilityRetriever | None = None,
        policy: MatchPolicy | None = None,
    ):
        self._catalog = catalog
        self._retriever = retriever or LexicalRetriever()
        self._policy = policy or MatchPolicy()

    def match(self, task: TaskSpec, facts: EnvironmentFacts) -> MatchResult:
        definitions = self._catalog.list_executable(task.tenant_id)
        result = MatchResult(n_catalog=len(definitions))
        shortlist = [
            s
            for s in self._retriever.rank(task, definitions)
            if s.similarity >= self._policy.min_similarity
        ][: self._policy.max_candidates]
        for s in shortlist:
            reasons = check_eligibility(s.definition, task, facts, self._catalog)
            if reasons:
                result.rejected.append(
                    Rejected(s.definition.id, s.definition.version, s.similarity, reasons)
                )
            else:
                result.eligible.append(EligibleCapability(s.definition, s.similarity))
        return result


__all__ = [
    "CapabilityMatcher",
    "CapabilityRetriever",
    "EligibleCapability",
    "LexicalRetriever",
    "MatchPolicy",
    "MatchResult",
    "Rejected",
    "ScoredCandidate",
    "check_eligibility",
]
