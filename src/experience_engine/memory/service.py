"""Build memory artifacts from discovery trajectories and register them as CANDIDATE versions.

Learning-plane code: it sees only `Trajectory` / `CapabilityDefinition` objects and the memory
store port, never the registry, evaluator or datasets (architecture test). Registration is
idempotent on the content hash; changed content becomes a new immutable version, like capabilities.
"""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from experience_engine.domain.capability import (
    CapabilityDefinition,
    FileRolePresent,
    PathRole,
    Precondition,
    RiskLevelAtMost,
    TaskFamilyIs,
    ToolsAvailable,
)
from experience_engine.domain.experience import DISCOVERY_SPLIT, PIPELINE_VERSION, Trajectory
from experience_engine.domain.memory import (
    EpisodeEntry,
    MemoryDefinition,
    MemoryProvenance,
    MemoryType,
    RetrievalSpec,
    memory_content_hash,
    memory_id_for,
)
from experience_engine.domain.outcome import OutcomeStatus
from experience_engine.memory.builders import (
    build_consolidated,
    build_instructions,
    consolidate_family,
    render_episode,
    render_instructions,
)
from experience_engine.ports.artifacts import ArtifactStore
from experience_engine.ports.experience import ExperienceStore
from experience_engine.ports.memory import MemoryStore

MIN_SUPPORT = 3  # verified successes per family, same evidence bar as the capability compiler
MAX_BANK_SUCCESSES = 6
MAX_BANK_FAILURES = 3
_RISK = {"low": 0, "medium": 1, "high": 2}


class RegistrationKind(StrEnum):
    NEW_MEMORY = "NEW_MEMORY"
    NEW_VERSION = "NEW_VERSION"
    NO_CHANGE = "NO_CHANGE"


@dataclass(frozen=True)
class Registered:
    memory_id: str
    version: int
    memory_type: MemoryType
    kind: RegistrationKind


@dataclass
class BuildResult:
    definitions: list[MemoryDefinition] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def load_experience(
    store: ExperienceStore, artifacts: ArtifactStore, tenant_id: str
) -> tuple[list[Trajectory], dict[str, str]]:
    """Eligible discovery trajectories and run_id -> trajectory artifact hash (provenance).

    The database CHECK already makes `eligible` imply the DISCOVERY split and a verified outcome;
    this re-asserts it so a schema regression cannot leak validation/hidden data into memory.
    """
    out: list[Trajectory] = []
    evidence: dict[str, str] = {}
    for case in store.list_eligible_cases(tenant_id, PIPELINE_VERSION):
        if case.dataset_split != DISCOVERY_SPLIT or not case.trajectory_sha256:
            raise RuntimeError(f"case {case.case_id} is not usable discovery experience")
        traj = Trajectory.model_validate_json(artifacts.get(tenant_id, case.trajectory_sha256))
        out.append(traj)
        evidence[traj.run_id] = case.trajectory_sha256
    return out, evidence


def _family_of(d: CapabilityDefinition) -> str | None:
    return next((p.family for p in d.preconditions if isinstance(p, TaskFamilyIs)), None)


def _conditions(
    family: str, ok: Sequence[Trajectory], all_trajs: Sequence[Trajectory]
) -> tuple[list[Precondition], list[Precondition]]:
    tools = sorted({tok.split(":")[0] for t in ok for tok in t.tool_tokens})
    shared_roles = sorted(set.intersection(*(set(t.file_roles) for t in ok)))
    risk = max((t.risk_level for t in ok), key=lambda r: _RISK.get(r, 0))
    applicable: list[Precondition] = [TaskFamilyIs(family=family)]
    if tools:
        applicable.append(ToolsAvailable(tools=tools))
    applicable += [FileRolePresent(role=PathRole(r)) for r in shared_roles]
    applicable.append(RiskLevelAtMost(level=risk))
    others = sorted({(t.task_family or "") for t in all_trajs} - {family, ""})
    return applicable, [TaskFamilyIs(family=o) for o in others]


def _limitations(
    family: str, ok: Sequence[Trajectory], bad: Sequence[Trajectory], others: Sequence[Precondition]
) -> tuple[list[str], list[str]]:
    failure = []
    if bad:
        claimed = sum(1 for t in bad if t.claimed_done)
        failure.append(
            f"{len(bad)} discovery runs failed independent verification; {claimed} of them claimed "
            "success"
        )
    limits = [
        f"Learned from {len(ok)} verified successes and {len(bad)} failures of family '{family}' "
        "only; not validated for any other family.",
        "Text for the general agent: it must still verify every constraint against the files.",
    ]
    for o in others:
        if isinstance(o, TaskFamilyIs):
            limits.append(f"Excluded for task family '{o.family}'.")
    return failure, limits


def build_memory(
    *,
    tenant_id: str,
    trajectories: Sequence[Trajectory],
    capabilities: Sequence[CapabilityDefinition],
    evidence: dict[str, str],
    now: datetime,
    dataset_name: str | None = None,
    dataset_version: str | None = None,
) -> BuildResult:
    """Deterministic: same trajectories -> same artifacts (hashes included)."""
    result = BuildResult()
    by_family: dict[str, list[Trajectory]] = defaultdict(list)
    for t in sorted(trajectories, key=lambda x: x.run_id):
        by_family[t.task_family or "unknown"].append(t)
    for family, trajs in sorted(by_family.items()):
        ok = [t for t in trajs if t.outcome_status == OutcomeStatus.VERIFIED_SUCCESS]
        bad = [t for t in trajs if t.outcome_status == OutcomeStatus.FAILURE]
        if family == "unknown" or len(ok) < MIN_SUPPORT:
            result.skipped.append(f"{family}: {len(ok)} verified successes < {MIN_SUPPORT}")
            continue
        applicable, exclusions = _conditions(family, ok, trajectories)
        failure, limits = _limitations(family, ok, bad, exclusions)
        sources = [t.run_id for t in ok + bad]
        refs = sorted({evidence[r] for r in sources if r in evidence})
        prov = MemoryProvenance(
            pipeline_version=PIPELINE_VERSION,
            dataset_name=dataset_name,
            dataset_version=dataset_version,
        )

        def make(
            mtype: MemoryType,
            content: str,
            *,
            episodes: list[EpisodeEntry] | None = None,
            retrieval: RetrievalSpec | None = None,
            cap_refs: list[str] | None = None,
            family: str = family,
            applicable: list[Precondition] = applicable,
            exclusions: list[Precondition] = exclusions,
            failure: list[str] = failure,
            limits: list[str] = limits,
            sources: list[str] = sources,
            refs: list[str] = refs,
            prov: MemoryProvenance = prov,
        ) -> MemoryDefinition:
            digest = memory_content_hash(
                memory_type=mtype,
                task_family=family,
                content=content,
                episodes=episodes or [],
                retrieval=retrieval,
                applicability_conditions=applicable,
                exclusion_conditions=exclusions,
                failure_conditions=failure,
                known_limitations=limits,
                source_capability_refs=cap_refs or [],
            )
            return MemoryDefinition(
                memory_id=memory_id_for(mtype, family),
                version=1,
                tenant_id=tenant_id,
                memory_type=mtype,
                task_family=family,
                content=content,
                episodes=episodes or [],
                retrieval=retrieval,
                applicability_conditions=applicable,
                exclusion_conditions=exclusions,
                failure_conditions=failure,
                known_limitations=limits,
                source_experience_ids=sources,
                evidence_refs=refs,
                source_capability_refs=cap_refs or [],
                provenance=prov,
                content_hash=digest,
                created_at=now,
            )

        bank = [
            EpisodeEntry(
                run_id=t.run_id,
                outcome=t.outcome_status.value,
                claimed_done=t.claimed_done,
                tool_tokens=t.tool_tokens,
                tokens_used=t.tokens_used,
                tool_calls=t.tool_calls,
                text=render_episode(t, 0)[:600],
            )
            for t in ok[:MAX_BANK_SUCCESSES] + bad[:MAX_BANK_FAILURES]
        ]
        spec = RetrievalSpec()
        result.definitions.append(
            make(
                MemoryType.EPISODIC,
                "Retrieval bank of normalised past episodes (verified successes and failures).",
                episodes=bank,
                retrieval=spec,
            )
        )
        result.definitions.append(
            make(MemoryType.CONSOLIDATED, build_consolidated([consolidate_family(family, trajs)]))
        )
        caps = [c for c in capabilities if _family_of(c) == family]
        if caps:
            cap = max(caps, key=lambda c: c.version)
            result.definitions.append(
                make(
                    MemoryType.PROCEDURAL,
                    build_instructions([render_instructions(cap)]),
                    cap_refs=[f"{cap.id}@{cap.version}"],
                )
            )
        else:
            result.skipped.append(f"{family}: no compiled capability, no PROCEDURAL memory")
    return result


def register_candidates(
    store: MemoryStore, definitions: Sequence[MemoryDefinition]
) -> list[Registered]:
    """Idempotent: unchanged content is NO_CHANGE; changed content is a new immutable version."""
    out: list[Registered] = []
    for d in definitions:
        latest = store.latest(d.tenant_id, d.memory_id)
        if latest is None:
            store.register(d.model_copy(update={"version": 1}))
            out.append(Registered(d.memory_id, 1, d.memory_type, RegistrationKind.NEW_MEMORY))
        elif latest.content_hash == d.content_hash:
            out.append(
                Registered(d.memory_id, latest.version, d.memory_type, RegistrationKind.NO_CHANGE)
            )
        else:
            version = latest.version + 1
            store.register(d.model_copy(update={"version": version}))
            out.append(
                Registered(d.memory_id, version, d.memory_type, RegistrationKind.NEW_VERSION)
            )
    return out
