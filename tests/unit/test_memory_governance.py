"""M5.8 unit tests: typed memory artifacts, builders, applicability, production-mode selection,
and memory evaluation under the same promotion policy as capabilities."""

from datetime import UTC, datetime
from typing import Any

import pytest
from test_evaluation_engine import LOW, base_runner, ds, out
from test_learning import STATUS, traj

from experience_engine.adapters.db.memory_catalog import StaticMemoryCatalog
from experience_engine.bench.dataset import DEFAULT_SEED, generate_split
from experience_engine.capabilities.applicability import EnvironmentFacts
from experience_engine.capabilities.tools import classify_path
from experience_engine.domain.benchmark import Split
from experience_engine.domain.capability import CapabilityState, TaskFamilyIs, ToolsAvailable
from experience_engine.domain.evaluation import Verdict
from experience_engine.domain.memory import (
    MemoryDefinition,
    MemoryType,
    memory_content_hash,
    verify_content_hash,
)
from experience_engine.evaluation.arms import ArmOutcome
from experience_engine.evaluation.memory_eval import evaluate_memory, make_memory_asset_arm
from experience_engine.memory.applicability import memory_violations
from experience_engine.memory.runtime import memory_text, select_memory
from experience_engine.memory.service import (
    RegistrationKind,
    build_memory,
    register_candidates,
)
from experience_engine.ports.memory import LatestMemory
from support import FakeClock, first_task

TENANT = "bench"
NOW = datetime(2025, 1, 1, tzinfo=UTC)
FULL = (
    "list_files",
    "read_file:config",
    "read_file:infra",
    "read_file:log",
    "set_json_value:config",
)
NO_LOG = ("list_files", "read_file:config", "read_file:infra", "set_json_value:config")


def experience() -> list:  # type: ignore[type-arg]
    return [
        *[traj(tokens=FULL) for _ in range(4)],
        *[traj(tokens=NO_LOG, status=STATUS.FAILURE) for _ in range(4)],
        *[traj(family="timeout", tokens=FULL) for _ in range(3)],
        *[traj(family="rare", tokens=FULL) for _ in range(2)],  # below the evidence bar
    ]


def cap_def() -> Any:
    from test_capability_runtime import make

    return make()


def built(data: list | None = None, **kw: Any) -> list[MemoryDefinition]:  # type: ignore[type-arg]
    data = data if data is not None else experience()
    evidence = {t.run_id: "e" * 64 for t in data}
    res = build_memory(
        tenant_id=TENANT, trajectories=data, capabilities=[cap_def()], evidence=evidence,
        now=NOW, **kw,
    )  # fmt: skip
    return res.definitions


def facts(bench: Any) -> EnvironmentFacts:
    roles = {classify_path(p) for p in bench.environment.files}
    t = bench.task
    return EnvironmentFacts.of(t.task_family, t.allowed_tools, t.risk_level, roles)


def pool_memory(kind: MemoryType = MemoryType.CONSOLIDATED) -> MemoryDefinition:
    return next(d for d in built() if d.task_family == "pool" and d.memory_type == kind)


# ---- typed artifact -------------------------------------------------------------------------
def test_builder_makes_three_typed_artifacts_per_qualifying_family_with_boundaries() -> None:
    defs = built()
    pool = [d for d in defs if d.task_family == "pool"]
    assert {d.memory_type for d in pool} == set(MemoryType)
    assert all(d.family_boundary == "pool" and d.version == 1 for d in pool)
    # 'rare' has 2 successes (< 3): no artifact; 'timeout' has no compiled capability: no procedural
    assert not any(d.task_family == "rare" for d in defs)
    assert {d.memory_type for d in defs if d.task_family == "timeout"} == {
        MemoryType.EPISODIC,
        MemoryType.CONSOLIDATED,
    }
    c = pool_memory()
    assert any(
        isinstance(x, TaskFamilyIs) and x.family == "timeout" for x in c.exclusion_conditions
    )
    assert any(isinstance(x, ToolsAvailable) for x in c.applicability_conditions)
    assert c.failure_conditions and any(
        "Excluded for task family" in x for x in c.known_limitations
    )


def test_episodic_artifact_is_a_versioned_bank_with_provenance_not_raw_values() -> None:
    e = pool_memory(MemoryType.EPISODIC)
    assert e.retrieval is not None and e.retrieval.max_episodes == 3
    assert {x.outcome for x in e.episodes} == {"VERIFIED_SUCCESS", "FAILURE"}
    assert len(e.source_experience_ids) == 8 and e.evidence_refs == ["e" * 64]
    text = memory_text(e)
    assert text.count("VERIFIED SUCCESS") == 2 and text.count("FAILED independent") == 1
    assert "Episode 1 " in text and "Episode 0 " not in text and "app/config" not in text


def test_procedural_memory_cites_its_capability_and_matches_the_compiled_steps() -> None:
    p = pool_memory(MemoryType.PROCEDURAL)
    assert p.source_capability_refs == ["pool.0123456789@1"]
    assert "1. List the files" in p.content and "Limitation:" in p.content


def test_build_is_deterministic_and_hash_covers_what_the_agent_sees() -> None:
    data = experience()
    a, b = built(data), built(list(reversed(data)))
    assert sorted(d.content_hash for d in a) == sorted(d.content_hash for d in b)
    c = pool_memory()
    assert verify_content_hash(c)
    changed = c.model_copy(update={"content": c.content + " extra"})
    assert not verify_content_hash(changed)
    assert c.content_hash == memory_content_hash(  # evidence ids/timestamps are not part of it
        memory_type=c.memory_type, task_family=c.task_family, content=c.content,
        episodes=c.episodes, retrieval=c.retrieval,
        applicability_conditions=c.applicability_conditions,
        exclusion_conditions=c.exclusion_conditions, failure_conditions=c.failure_conditions,
        known_limitations=c.known_limitations, source_capability_refs=c.source_capability_refs,
    )  # fmt: skip


def test_schema_rejects_inconsistent_artifacts() -> None:
    c = pool_memory()
    with pytest.raises(ValueError, match="retrieval bank"):
        pool_memory(MemoryType.EPISODIC).model_copy(update={"episodes": []}).model_validate(
            {**pool_memory(MemoryType.EPISODIC).model_dump(), "episodes": []}
        )
    with pytest.raises(ValueError, match="only EPISODIC"):
        MemoryDefinition.model_validate({**c.model_dump(), "retrieval": {"max_episodes": 2}})
    with pytest.raises(ValueError, match="PROCEDURAL"):
        MemoryDefinition.model_validate(
            {**pool_memory(MemoryType.PROCEDURAL).model_dump(), "source_capability_refs": []}
        )
    with pytest.raises(ValueError):
        MemoryDefinition.model_validate({**c.model_dump(), "memory_id": "not a valid id"})
    with pytest.raises(ValueError):
        MemoryDefinition.model_validate({**c.model_dump(), "extra_field": 1})


# ---- registration ---------------------------------------------------------------------------
class FakeStore:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, int], MemoryDefinition] = {}

    def latest(self, tenant_id: str, memory_id: str) -> LatestMemory | None:
        vs = [v for (t, m, v) in self.rows if t == tenant_id and m == memory_id]
        if not vs:
            return None
        d = self.rows[(tenant_id, memory_id, max(vs))]
        return LatestMemory(d.version, d.content_hash, CapabilityState.CANDIDATE)

    def register(self, definition: MemoryDefinition) -> bool:
        self.rows[(definition.tenant_id, definition.memory_id, definition.version)] = definition
        return True


def test_registration_is_idempotent_and_changed_content_is_a_new_immutable_version() -> None:
    store = FakeStore()
    data = experience()
    first = register_candidates(store, built(data))  # type: ignore[arg-type]
    assert {r.kind for r in first} == {RegistrationKind.NEW_MEMORY} and len(store.rows) == 5
    again = register_candidates(store, built(data))  # type: ignore[arg-type]
    assert {r.kind for r in again} == {RegistrationKind.NO_CHANGE} and len(store.rows) == 5
    edited = [
        d.model_copy(update={"content": d.content + " more", "content_hash": "f" * 64})
        if d.memory_type == MemoryType.CONSOLIDATED and d.task_family == "pool"
        else d
        for d in built(data)
    ]
    third = register_candidates(store, edited)  # type: ignore[arg-type]
    bumped = [r for r in third if r.kind == RegistrationKind.NEW_VERSION]
    assert [(r.memory_id, r.version) for r in bumped] == [("mem_consolidated_pool", 2)]


# ---- applicability is typed, never similarity -----------------------------------------------
def test_memory_applies_only_inside_its_declared_boundary() -> None:
    d = pool_memory()
    pool_task = first_task()
    timeout_task = next(
        t for t in generate_split(Split.DISCOVERY, DEFAULT_SEED) if t.family == "timeout"
    )
    assert memory_violations(d, facts(pool_task)) == []
    assert any("task_family" in r for r in memory_violations(d, facts(timeout_task)))
    no_tools = pool_task.task.model_copy(update={"allowed_tools": ["list_files"]})
    f = EnvironmentFacts.of("pool", no_tools.allowed_tools, "low", facts(pool_task).file_roles)
    assert any("tools not allowed" in r for r in memory_violations(d, f))


def test_exclusion_conditions_and_tampering_withhold_the_memory() -> None:
    d = pool_memory()
    f = facts(first_task())
    excluded = d.model_copy(update={"exclusion_conditions": [TaskFamilyIs(family="pool")]})
    # (hash no longer matches because exclusions are part of it) -> refused as tampered
    assert "tampered" in memory_violations(excluded, f)[0]
    rehash = memory_content_hash(
        memory_type=d.memory_type, task_family=d.task_family, content=d.content, episodes=[],
        retrieval=None, applicability_conditions=d.applicability_conditions,
        exclusion_conditions=[TaskFamilyIs(family="pool")], failure_conditions=d.failure_conditions,
        known_limitations=d.known_limitations, source_capability_refs=[],
    )  # fmt: skip
    legit = excluded.model_copy(update={"content_hash": rehash})
    assert memory_violations(legit, f) == ["excluded: task_family_is holds for this task"]


def test_an_artifact_without_a_family_boundary_is_representable_but_applies_everywhere() -> None:
    d = pool_memory()
    conds = [c for c in d.applicability_conditions if not isinstance(c, TaskFamilyIs)]
    h = memory_content_hash(
        memory_type=d.memory_type, task_family=d.task_family, content=d.content, episodes=[],
        retrieval=None, applicability_conditions=conds, exclusion_conditions=[],
        failure_conditions=[], known_limitations=[], source_capability_refs=[],
    )  # fmt: skip
    wide = d.model_copy(
        update={"applicability_conditions": conds, "exclusion_conditions": [], "content_hash": h,
                "failure_conditions": [], "known_limitations": []}
    )  # fmt: skip
    assert wide.family_boundary is None
    other = next(t for t in generate_split(Split.DISCOVERY, DEFAULT_SEED) if t.family == "timeout")
    assert memory_violations(wide, facts(other)) == []  # nothing stops it: the gate must


# ---- production mode: ACTIVE-only selection with explicit fallback --------------------------
def test_selection_uses_only_what_the_active_catalog_returns_and_reports_why_not() -> None:
    d = pool_memory()
    f = facts(first_task())
    sel = select_memory(StaticMemoryCatalog(TENANT, [d]), TENANT, MemoryType.CONSOLIDATED, f)
    assert (
        sel.artifact == d and sel.ref == "mem_consolidated_pool@1" and sel.fallback_reason is None
    )
    assert sel.text == d.content and sel.retrieved_tokens > 0
    empty = select_memory(StaticMemoryCatalog(TENANT, []), TENANT, MemoryType.CONSOLIDATED, f)
    assert empty.artifact is None and "no ACTIVE CONSOLIDATED memory" in (
        empty.fallback_reason or ""
    )
    wrong_type = select_memory(StaticMemoryCatalog(TENANT, [d]), TENANT, MemoryType.EPISODIC, f)
    assert wrong_type.artifact is None
    other = next(t for t in generate_split(Split.DISCOVERY, DEFAULT_SEED) if t.family == "timeout")
    na = select_memory(
        StaticMemoryCatalog(TENANT, [d]), TENANT, MemoryType.CONSOLIDATED, facts(other)
    )
    assert na.artifact is None and "no applicable ACTIVE" in (na.fallback_reason or "")
    assert na.considered and na.considered[0][0] == "mem_consolidated_pool@1"


def test_selection_is_tenant_scoped() -> None:
    d = pool_memory()
    sel = select_memory(
        StaticMemoryCatalog("other-tenant", [d]),
        TENANT,
        MemoryType.CONSOLIDATED,
        facts(first_task()),
    )
    assert sel.artifact is None


# ---- evaluation under the same promotion policy ---------------------------------------------
def memory_runner(solves: set[str], tokens: int = 100):  # type: ignore[no-untyped-def]
    def run(task):  # type: ignore[no-untyped-def]
        return out(task.variant in solves, tokens, applicable=True)

    return run


def eval_memory(d: MemoryDefinition, arm: Any, baseline: Any, **kw: Any):  # type: ignore[no-untyped-def]
    by = {s: generate_split(s, DEFAULT_SEED) for s in Split}
    return evaluate_memory(
        definition=d, policy=LOW, clock=FakeClock(), baseline=baseline, memory_arm=arm,
        source_tasks=by[Split.DISCOVERY], heldout_tasks=by[Split.VALIDATION],
        transfer_tasks=by[Split.HIDDEN_TRANSFER], other_family_tasks=by[Split.VALIDATION],
        datasets={s.value: ds(s) for s in Split}, verifier_version="v",
        model_settings={"provider": "fake", "model": "m"}, seeds=[0], **kw,
    )  # fmt: skip


def gated_arm(d: MemoryDefinition, solves: set[str]):  # type: ignore[no-untyped-def]
    inner = memory_runner(solves)

    def run(task):  # type: ignore[no-untyped-def]
        from experience_engine.evaluation.memory_eval import facts_of

        if memory_violations(d, facts_of(task)):
            return ArmOutcome("NOT_APPLICABLE", False, 0, 0, 0, applicable=False)
        return inner(task)

    return run


KNOWN = {"sqlalchemy", "psycopg_pool"}
ALL = KNOWN | {"hikari"}


def test_a_family_scoped_memory_that_helps_passes_the_unchanged_policy() -> None:
    d = pool_memory()
    rep = eval_memory(d, gated_arm(d, ALL), base_runner(KNOWN))
    assert rep.verdict == Verdict.PASS, rep.reasons
    assert rep.asset_kind == "MEMORY" and rep.treatment_arm == "M_MEMORY_ASSET"
    assert rep.capability_id == "mem_consolidated_pool" and rep.policy_hash == LOW.policy_hash()
    assert rep.false_applicability_rate == 0.0 and rep.regressions == 0
    assert set(rep.suites["TRANSFER"]) == {"A_BASELINE", "M_MEMORY_ASSET"}


def test_memory_that_cannot_identify_its_boundary_is_rejected_for_false_applicability() -> None:
    d = pool_memory()
    # an "unrestricted" artifact: it injects everywhere (modelled as an arm that never refuses)
    rep = eval_memory(d, memory_runner(ALL), base_runner(KNOWN))
    assert rep.verdict == Verdict.FAIL
    assert rep.false_applicability_rate and rep.false_applicability_rate > 0
    assert any("false applicability" in r for r in rep.reasons)


def test_memory_that_only_helps_on_its_source_examples_is_rejected() -> None:
    d = pool_memory()
    rep = eval_memory(d, gated_arm(d, set()), base_runner(KNOWN))  # helps nowhere on unseen tasks
    assert rep.verdict == Verdict.FAIL
    assert any("held-out" in r or "transfer" in r for r in rep.reasons)


def test_memory_that_causes_regressions_is_rejected_even_if_it_scores_well() -> None:
    d = pool_memory()
    calls: dict[str, int] = {}

    def flaky(task):  # type: ignore[no-untyped-def]
        calls[task.task.task_id] = calls.get(task.task.task_id, 0) + 1
        ok = task.variant in ALL and not task.task.task_id.endswith(("-00", "-01", "-02"))
        return out(ok, 100, applicable=True)

    rep = eval_memory(d, flaky, base_runner(ALL))  # baseline solves everything
    assert rep.regressions > LOW.max_regressions and rep.verdict == Verdict.FAIL


def test_memory_asset_arm_refuses_tasks_outside_the_boundary_without_calling_the_model() -> None:
    from experience_engine.adapters.llm.fake import HeuristicFakeLLM
    from experience_engine.bench.verifier import SimulationVerifier
    from support import config

    d = pool_memory()
    calls: list[int] = []

    class Spy(HeuristicFakeLLM):
        def complete(self, request):  # type: ignore[no-untyped-def]
            calls.append(1)
            return super().complete(request)

    arm = make_memory_asset_arm(
        definition=d, llm=Spy(), config=config(), verifier=SimulationVerifier(), clock=FakeClock()
    )
    other = next(t for t in generate_split(Split.DISCOVERY, DEFAULT_SEED) if t.family == "timeout")
    refused = arm(other)
    assert (refused.applicable, refused.verified, refused.tokens) == (False, False, 0) and not calls
    done = arm(first_task())
    assert done.applicable is True and done.verified and calls and done.retrieved_tokens > 0


def test_shared_baseline_is_computed_once_per_task_and_repetition() -> None:
    from experience_engine.evaluation.arms import SharedArm

    runs: list[str] = []

    def inner(task):  # type: ignore[no-untyped-def]
        runs.append(task.task.task_id)
        return out(True, 10)

    shared = SharedArm(inner)
    d = pool_memory()
    a = eval_memory(d, gated_arm(d, ALL), shared)
    b = eval_memory(d, gated_arm(d, ALL), shared)  # a second asset reuses the same draws
    assert a.verdict == b.verdict and shared.computed == len(runs)
    assert shared.served > shared.computed
