"""Learned memory as a first-class, versioned asset (pure data).

Deliberately separate from `CapabilityDefinition`: memory is *text handed to the general agent*,
a capability is an *executable procedure*. They share only the lifecycle (`CapabilityState`, used
as the asset state), the typed applicability conditions and the promotion policy. Lifecycle fields
(state, evaluations, pointers) live in the database version row; this body is immutable and
content-hashed.
"""

import hashlib
import json
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from experience_engine.domain.capability import (
    CapabilityState,
    Precondition,
    TaskFamilyIs,
)
from experience_engine.domain.time import UtcDatetime

# The lifecycle is shared; "asset state" names the role it plays for both asset kinds.
AssetState = CapabilityState
MAX_CONTENT_CHARS = 6000
MEMORY_BUILDER_VERSION = "memory-builder/1"


class AssetKind(StrEnum):
    CAPABILITY = "CAPABILITY"
    MEMORY = "MEMORY"


class MemoryType(StrEnum):
    EPISODIC = "EPISODIC"
    CONSOLIDATED = "CONSOLIDATED"
    PROCEDURAL = "PROCEDURAL"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EpisodeEntry(_Frozen):
    """One normalised past episode in a retrieval bank (no raw paths or values)."""

    run_id: str
    outcome: str  # OutcomeStatus value
    claimed_done: bool
    tool_tokens: list[str]
    tokens_used: int
    tool_calls: int
    text: str = Field(max_length=600)


class RetrievalSpec(_Frozen):
    """How an episodic bank is queried: deterministic, never similarity-only."""

    max_episodes: int = Field(default=3, ge=1, le=8)
    include_failure: bool = True
    ranking: str = "success_first,run_id"


class MemoryProvenance(_Frozen):
    builder_version: str = MEMORY_BUILDER_VERSION
    pipeline_version: str
    dataset_name: str | None = None
    dataset_version: str | None = None


class MemoryDefinition(_Frozen):
    """Immutable body of one memory version."""

    memory_id: Annotated[str, Field(pattern=r"^mem_[a-z]+_[a-z0-9_]{1,40}$")]
    version: int = Field(ge=1)
    tenant_id: str = Field(min_length=1)
    memory_type: MemoryType
    task_family: str = Field(min_length=1, max_length=40)
    content: str = Field(min_length=1, max_length=MAX_CONTENT_CHARS)
    episodes: list[EpisodeEntry] = Field(default_factory=list)  # EPISODIC only
    retrieval: RetrievalSpec | None = None
    applicability_conditions: list[Precondition]
    exclusion_conditions: list[Precondition] = Field(default_factory=list)
    failure_conditions: list[str] = Field(default_factory=list, max_length=20)
    known_limitations: list[str] = Field(default_factory=list, max_length=20)
    source_experience_ids: list[str] = Field(min_length=1)  # run ids of the discovery trajectories
    evidence_refs: list[str] = Field(default_factory=list)  # artifact sha256 of trajectories
    source_capability_refs: list[str] = Field(default_factory=list)  # PROCEDURAL: id@version
    provenance: MemoryProvenance
    content_hash: str = Field(min_length=64, max_length=64)
    created_at: UtcDatetime

    @model_validator(mode="after")
    def _consistent(self) -> "MemoryDefinition":
        if self.memory_type == MemoryType.EPISODIC:
            if not self.episodes or self.retrieval is None:
                raise ValueError("an EPISODIC memory needs a retrieval bank and a retrieval spec")
        elif self.episodes or self.retrieval is not None:
            raise ValueError("only EPISODIC memory carries a retrieval bank")
        if self.memory_type == MemoryType.PROCEDURAL and not self.source_capability_refs:
            raise ValueError("PROCEDURAL memory must cite the capability it was rendered from")
        return self

    @property
    def family_boundary(self) -> str | None:
        """The declared task family, or None if the artifact does not restrict itself to one."""
        return next(
            (p.family for p in self.applicability_conditions if isinstance(p, TaskFamilyIs)), None
        )


def memory_content_hash(
    *,
    memory_type: MemoryType,
    task_family: str,
    content: str,
    episodes: list[EpisodeEntry],
    retrieval: RetrievalSpec | None,
    applicability_conditions: list[Precondition],
    exclusion_conditions: list[Precondition],
    failure_conditions: list[str],
    known_limitations: list[str],
    source_capability_refs: list[str],
) -> str:
    """Hash of everything that affects what the agent sees or when it sees it. Evidence ids and
    timestamps are excluded so adding provenance alone never creates a new version."""
    body = {
        "type": memory_type.value,
        "family": task_family,
        "content": content,
        "episodes": [e.model_dump(mode="json") for e in episodes],
        "retrieval": retrieval.model_dump(mode="json") if retrieval else None,
        "applicability": [c.model_dump(mode="json") for c in applicability_conditions],
        "exclusions": [c.model_dump(mode="json") for c in exclusion_conditions],
        "failure_conditions": failure_conditions,
        "limitations": known_limitations,
        "capabilities": source_capability_refs,
    }
    blob = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def verify_content_hash(d: MemoryDefinition) -> bool:
    return d.content_hash == memory_content_hash(
        memory_type=d.memory_type,
        task_family=d.task_family,
        content=d.content,
        episodes=d.episodes,
        retrieval=d.retrieval,
        applicability_conditions=d.applicability_conditions,
        exclusion_conditions=d.exclusion_conditions,
        failure_conditions=d.failure_conditions,
        known_limitations=d.known_limitations,
        source_capability_refs=d.source_capability_refs,
    )


def memory_id_for(memory_type: MemoryType, task_family: str) -> str:
    fam = "".join(c if c.isalnum() else "_" for c in task_family.lower())[:40].strip("_")
    return f"mem_{memory_type.value.lower()}_{fam}"
