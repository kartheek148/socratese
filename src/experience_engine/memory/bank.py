"""Retrieval over discovery experience. Deterministic and advisory: it ranks, it never decides
whether a procedure may run (that is the capability runtime's eligibility gate)."""

import time
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from experience_engine.domain.capability import CapabilityDefinition, TaskFamilyIs
from experience_engine.domain.experience import Trajectory
from experience_engine.domain.outcome import OutcomeStatus
from experience_engine.memory.builders import (
    build_consolidated,
    build_episodic,
    build_instructions,
    consolidate_family,
    estimate_tokens,
    render_instructions,
)

MAX_EPISODES = 3  # two successes and one failure when a failure exists


@dataclass(frozen=True)
class TaskQuery:
    family: str | None
    roles: frozenset[str]
    tools: frozenset[str]


@dataclass(frozen=True)
class Retrieved:
    text: str
    items: int
    retrieved_tokens: int
    retrieval_ms: int


def _score(q: TaskQuery, t: Trajectory) -> tuple[int, int, int]:
    return (
        int(q.family is not None and t.task_family == q.family),
        len(q.roles & set(t.file_roles)),
        len(q.tools & set(t.allowed_tools)),
    )


def _pack(text: str, items: int, started: float) -> Retrieved:
    ms = int((time.perf_counter() - started) * 1000)
    return Retrieved(text, items, estimate_tokens(text), ms)


class ExperienceMemory:
    """Built once from eligible discovery trajectories (+ compiled definitions for arm D)."""

    def __init__(
        self,
        trajectories: Sequence[Trajectory],
        definitions: Sequence[CapabilityDefinition] = (),
    ):
        self._trajs = sorted(trajectories, key=lambda t: t.run_id)
        self._definitions = list(definitions)
        by_family: dict[str, list[Trajectory]] = defaultdict(list)
        for t in self._trajs:
            by_family[t.task_family or "unknown"].append(t)
        started = time.perf_counter()
        self._summaries = {f: consolidate_family(f, ts) for f, ts in sorted(by_family.items())}
        self.consolidation_ms = int((time.perf_counter() - started) * 1000)
        self._rendered = {
            next((p.family for p in d.preconditions if isinstance(p, TaskFamilyIs)), "?"): (
                render_instructions(d)
            )
            for d in self._definitions
        }

    @property
    def families(self) -> list[str]:
        return sorted(self._summaries)

    def episodic(self, q: TaskQuery) -> Retrieved:
        started = time.perf_counter()
        ranked = sorted(self._trajs, key=lambda t: (tuple(-x for x in _score(q, t)), t.run_id))
        ok = [t for t in ranked if t.outcome_status == OutcomeStatus.VERIFIED_SUCCESS]
        bad = [t for t in ranked if t.outcome_status == OutcomeStatus.FAILURE]
        chosen = ok[: MAX_EPISODES - 1] + bad[:1] if bad else ok[:MAX_EPISODES]
        return _pack(build_episodic(chosen), len(chosen), started)

    def _by_family(self, q: TaskQuery, table: dict[str, str]) -> list[str]:
        if q.family in table:
            return [table[q.family]]
        # unseen family: the nearest experience is everything we know (advisory, labelled)
        return [table[f] for f in sorted(table)]

    def consolidated(self, q: TaskQuery) -> Retrieved:
        started = time.perf_counter()
        parts = self._by_family(q, self._summaries)
        return _pack(build_consolidated(parts), len(parts), started)

    def instructions(self, q: TaskQuery) -> Retrieved:
        started = time.perf_counter()
        parts = self._by_family(q, self._rendered)
        return _pack(build_instructions(parts), len(parts), started)
