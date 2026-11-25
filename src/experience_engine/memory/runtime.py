"""Production-mode memory selection: only ACTIVE versions, applicability-checked, with an explicit
fallback reason. Retrieval text is built from the immutable artifact; nothing here can promote,
demote or read CANDIDATE / VALIDATING / REJECTED content (the catalog port cannot return it)."""

import time
from dataclasses import dataclass, field

from experience_engine.capabilities.applicability import EnvironmentFacts
from experience_engine.domain.memory import MemoryDefinition, MemoryType
from experience_engine.memory.applicability import memory_violations
from experience_engine.memory.builders import PREAMBLE, estimate_tokens, render_episode_entry
from experience_engine.ports.memory import ActiveMemoryCatalog


@dataclass(frozen=True)
class MemorySelection:
    artifact: MemoryDefinition | None
    text: str = ""
    retrieved_tokens: int = 0
    retrieval_ms: int = 0
    fallback_reason: str | None = None
    considered: list[tuple[str, list[str]]] = field(default_factory=list)

    @property
    def ref(self) -> str | None:
        a = self.artifact
        return f"{a.memory_id}@{a.version}" if a else None


def memory_text(d: MemoryDefinition) -> str:
    """The exact text the agent sees for this artifact."""
    if d.memory_type != MemoryType.EPISODIC or d.retrieval is None:
        return d.content
    ok = sorted((e for e in d.episodes if e.outcome == "VERIFIED_SUCCESS"), key=lambda e: e.run_id)
    bad = sorted((e for e in d.episodes if e.outcome != "VERIFIED_SUCCESS"), key=lambda e: e.run_id)
    n = d.retrieval.max_episodes
    chosen = ok[: n - 1] + bad[:1] if bad and d.retrieval.include_failure else ok[:n]
    body = "\n".join(render_episode_entry(e, i + 1) for i, e in enumerate(chosen))
    return f"{PREAMBLE}\nPast episodes:\n{body}"


def select_memory(
    catalog: ActiveMemoryCatalog,
    tenant_id: str,
    memory_type: MemoryType,
    facts: EnvironmentFacts,
) -> MemorySelection:
    started = time.perf_counter()
    active = [d for d in catalog.list_active(tenant_id) if d.memory_type == memory_type]
    considered: list[tuple[str, list[str]]] = []
    for d in sorted(active, key=lambda x: x.memory_id):
        reasons = memory_violations(d, facts)
        considered.append((f"{d.memory_id}@{d.version}", reasons))
        if not reasons:
            text = memory_text(d)
            ms = int((time.perf_counter() - started) * 1000)
            return MemorySelection(d, text, estimate_tokens(text), ms, None, considered)
    ms = int((time.perf_counter() - started) * 1000)
    if not active:
        why = f"no ACTIVE {memory_type.value} memory for this tenant"
    else:
        why = f"no applicable ACTIVE {memory_type.value} memory: " + "; ".join(
            f"{ref}: {r[0]}" for ref, r in considered if r
        )
    return MemorySelection(None, "", 0, ms, why[:400], considered)
