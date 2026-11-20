import itertools
import json
import random
from datetime import UTC, datetime
from pathlib import Path

import pytest

from experience_engine.adapters.artifacts.local_fs import LocalFsArtifactStore
from experience_engine.adapters.llm.fake import HeuristicFakeLLM
from experience_engine.bench.dataset import DEFAULT_SEED, generate_split
from experience_engine.domain.benchmark import Split
from experience_engine.domain.capability import RegistrationKind, procedure_hash
from experience_engine.domain.experience import (
    DISCOVERY_SPLIT,
    ExclusionReason,
    ExperienceCase,
    FindingKind,
    NormalizedStep,
    Trajectory,
    classify_eligibility,
)
from experience_engine.domain.ids import new_id
from experience_engine.domain.outcome import OutcomeStatus
from experience_engine.domain.run import RunStatus
from experience_engine.learning.compiler import CompilerConfig, compile_candidates
from experience_engine.learning.contrast import contrast
from experience_engine.learning.normalizer import IncompleteTraceError, build_trajectory
from experience_engine.learning.pipeline import (
    ExperiencePipeline,
    LearningJobProcessor,
    RunNotFoundError,
)
from experience_engine.ports.experience import ClaimedJob
from experience_engine.ports.persistence import RunReconstruction
from support import FakeClock, InMemoryLifecycle, ScriptedLLM, persisted_orchestrator, session

STATUS = OutcomeStatus


def traj(
    family: str = "pool",
    tokens: tuple[str, ...] = (
        "list_files",
        "read_file:log",
        "read_file:config",
        "read_file:infra",
        "set_json_value:config",
    ),
    status: OutcomeStatus = STATUS.VERIFIED_SUCCESS,
    tenant: str = "t",
    claimed: bool = True,
    shape: str | None = "abc",
    run_id: str | None = None,
) -> Trajectory:
    steps = [
        NormalizedStep(
            index=i,
            kind="tool",
            token=tok,
            tool=tok.split(":")[0],
            role=(tok.split(":")[1] if ":" in tok else None),  # type: ignore[arg-type]
            ok=True,
            value_type="number" if tok.startswith("set_json") else None,
        )
        for i, tok in enumerate(tokens)
    ]
    return Trajectory(
        run_id=run_id or new_id(),
        tenant_id=tenant,
        task_id="k",
        task_family=family,
        outcome_status=status,
        steps=steps,
        tokens_used=3000,
        tool_calls=len(tokens),
        claimed_done=claimed,
        termination="FINAL",
        risk_level="low",
        allowed_tools=["list_files"],
        file_roles=["config", "infra", "log"],
        config_shape=shape,
    )


def successes(n: int = 4, **kw: object) -> list[Trajectory]:
    return [traj(**kw) for _ in range(n)]  # type: ignore[arg-type]


# ---- eligibility (rule 9 and the discovery-only boundary) -----------------------------------
@pytest.mark.parametrize(
    ("status", "split", "completed", "eligible", "reason"),
    [
        (STATUS.VERIFIED_SUCCESS, "DISCOVERY", True, True, None),
        (STATUS.FAILURE, "DISCOVERY", True, True, None),
        (STATUS.PARTIAL, "DISCOVERY", True, False, ExclusionReason.UNVERIFIED_OUTCOME),
        (STATUS.UNKNOWN, "DISCOVERY", True, False, ExclusionReason.UNVERIFIED_OUTCOME),
        (STATUS.VERIFIED_SUCCESS, "VALIDATION", True, False, ExclusionReason.NON_DISCOVERY_SPLIT),
        (
            STATUS.VERIFIED_SUCCESS,
            "HIDDEN_TRANSFER",
            True,
            False,
            ExclusionReason.NON_DISCOVERY_SPLIT,
        ),
        (STATUS.VERIFIED_SUCCESS, None, True, False, ExclusionReason.UNKNOWN_PROVENANCE),
        (STATUS.VERIFIED_SUCCESS, "DISCOVERY", False, False, ExclusionReason.INCOMPLETE_RUN),
    ],
)
def test_eligibility_rules(status, split, completed, eligible, reason) -> None:  # type: ignore[no-untyped-def]
    assert classify_eligibility(status, split, completed) == (eligible, reason)


# ---- contrastive analysis -------------------------------------------------------------------
def test_contrast_classifies_critical_avoidable_and_neutral() -> None:
    ok = successes(5)
    bad = [
        traj(
            tokens=("list_files", "read_file:config", "set_json_value:config", "read_file:config"),
            status=STATUS.FAILURE,
        )
        for _ in range(4)
    ]
    by = {f.token: f for f in contrast(ok, bad)}
    assert by["read_file:infra"].kind == FindingKind.CRITICAL
    assert (
        by["read_file:infra"].success_support == 1.0
        and by["read_file:infra"].failure_support == 0.0
    )
    assert by["list_files"].kind == FindingKind.NEUTRAL  # present in both
    only_failures = contrast(
        ok, [traj(tokens=("list_files", "write_file:config"), status=STATUS.FAILURE)] * 3
    )
    assert {f.token: f.kind for f in only_failures}["write_file:config"] == FindingKind.AVOIDABLE


def test_contrast_without_counterexamples_claims_nothing() -> None:
    assert {f.kind for f in contrast(successes(4), [])} == {FindingKind.NEUTRAL}


# ---- compiler -------------------------------------------------------------------------------
def test_compiles_valid_traceable_candidate_with_versions_recorded() -> None:
    ok = successes(4)
    bad = [
        traj(
            tokens=("list_files", "read_file:config", "set_json_value:config"),
            status=STATUS.FAILURE,
        )
        for _ in range(2)
    ]
    result = compile_candidates(ok + bad, CompilerConfig(dataset_name="d", dataset_version="1"))
    assert not result.rejections and len(result.candidates) == 1
    b = result.candidates[0]
    d = b.definition
    assert d.id.startswith("pool.") and d.version == 1
    assert sorted(d.provenance.source_run_ids) == sorted(t.run_id for t in ok)
    assert sorted(d.provenance.counterexample_run_ids) == sorted(t.run_id for t in bad)
    assert (
        d.provenance.compiler_version == "rule-compiler/2"
        and d.provenance.pipeline_version == "experience/1"
    )
    assert d.provenance.prompt_version is None and d.provenance.model_version is None
    assert (d.provenance.dataset_name, d.provenance.dataset_version) == ("d", "1")
    ops = [s.op for s in d.steps]
    assert ops == [
        "TOOL_CALL",
        "TOOL_CALL",
        "TOOL_CALL",
        "TOOL_CALL",
        "LLM_DECISION",
        "TOOL_CALL",
        "VERIFY",
    ]
    kinds = {p.kind for p in d.preconditions}
    assert {"task_family_is", "tools_available", "file_role_present", "risk_level_at_most"} <= kinds
    assert d.permissions == ["list_files", "read_file", "set_json_value"]
    assert any("Hypothesis" in x and "read_file:infra" in x for x in d.known_limitations)
    assert any(t.kind == "SOURCE_REPLAY" for t in b.tests) and any(
        t.kind == "COUNTEREXAMPLE" for t in b.tests
    )
    assert any(t.kind == "NEGATIVE_APPLICABILITY" for t in b.tests)
    assert all(t.task_id is None or t.run_id in {x.run_id for x in ok + bad} for t in b.tests)


def test_decision_step_cap_leaves_room_for_thinking_models_and_is_configurable() -> None:
    from experience_engine.domain.capability import LlmDecision

    def cap(cfg: CompilerConfig) -> int:
        steps = compile_candidates(successes(4), cfg).candidates[0].definition.steps
        return next(s for s in steps if isinstance(s, LlmDecision)).max_tokens

    assert cap(CompilerConfig()) == 4000  # 2000 caused empty replies with a thinking model
    assert cap(CompilerConfig(decision_max_tokens=1500)) == 1500


def test_generation_is_deterministic_and_order_independent() -> None:
    data = successes(5) + [
        traj(tokens=("list_files", "set_json_value:config"), status=STATUS.FAILURE)
        for _ in range(2)
    ]
    a = compile_candidates(data).candidates[0].model_dump_json()
    shuffled = data[:]
    random.Random(7).shuffle(shuffled)
    assert compile_candidates(shuffled).candidates[0].model_dump_json() == a
    assert compile_candidates(data).candidates[0].model_dump_json() == a


def test_no_candidate_without_enough_verified_support() -> None:
    result = compile_candidates(successes(2))
    assert not result.candidates and "insufficient_support" in result.rejections[0].reason
    only_failures = [traj(status=STATUS.FAILURE) for _ in range(6)]
    assert not compile_candidates(only_failures).candidates


def test_unverified_outcomes_are_never_used_as_successes() -> None:
    mixed = (
        successes(2)
        + [traj(status=STATUS.UNKNOWN) for _ in range(5)]
        + [traj(status=STATUS.PARTIAL) for _ in range(5)]
    )
    assert not compile_candidates(mixed).candidates


def test_read_only_and_unapproved_tool_skeletons_are_rejected() -> None:
    read_only = successes(4, tokens=("list_files", "read_file:config"))
    assert "no_mutating_step" in compile_candidates(read_only).rejections[0].reason
    unapproved = successes(4, tokens=("list_files", "write_file:config"))
    assert "unapproved_tool: write_file" in compile_candidates(unapproved).rejections[0].reason


def test_low_support_steps_are_dropped_from_the_skeleton() -> None:
    # read_file:infra appears in 5 of 10 verified successes (50%)
    data = successes(5) + [
        traj(tokens=("list_files", "read_file:config", "set_json_value:config")) for _ in range(5)
    ]
    default = [s.id for s in compile_candidates(data).candidates[0].definition.steps]
    assert "read_infra" not in default  # below the default 0.6 support threshold
    lenient = compile_candidates(data, CompilerConfig(skeleton_support=0.5)).candidates[0]
    assert "read_infra" in [s.id for s in lenient.definition.steps]
    assert lenient.definition.id != compile_candidates(data).candidates[0].definition.id


def test_families_compile_separately_and_reference_each_other_in_negative_tests() -> None:
    pool = successes(4, family="pool")
    timeout = successes(
        4, family="timeout", tokens=("list_files", "read_file:log", "set_json_value:config")
    )
    result = compile_candidates(pool + timeout)
    assert sorted(c.task_family for c in result.candidates) == ["pool", "timeout"]
    pool_bundle = next(c for c in result.candidates if c.task_family == "pool")
    assert {
        p.family for p in pool_bundle.definition.preconditions if p.kind == "task_family_is"
    } == {"pool"}  # type: ignore[union-attr]
    other = [t for t in pool_bundle.tests if t.kind == "NEGATIVE_APPLICABILITY" and t.run_id]
    assert other and other[0].run_id in {t.run_id for t in timeout}


def test_cross_tenant_input_is_refused() -> None:
    with pytest.raises(ValueError, match="per tenant"):
        compile_candidates(successes(3, tenant="a") + successes(3, tenant="b"))


def test_procedure_hash_ignores_added_evidence_but_not_a_changed_procedure() -> None:
    base = compile_candidates(successes(4)).candidates[0]
    more = compile_candidates(successes(7)).candidates[0]
    assert base.definition.id == more.definition.id and base.procedure_hash == more.procedure_hash
    assert base.definition.provenance.source_run_ids != more.definition.provenance.source_run_ids
    other_procedure = compile_candidates(
        successes(4, tokens=("list_files", "read_file:config", "set_json_value:config"))
    ).candidates[0]
    assert other_procedure.definition.id != base.definition.id
    v2 = compile_candidates(
        successes(4), CompilerConfig(compiler_version="rule-compiler/3")
    ).candidates[0]
    assert v2.definition.id == base.definition.id and v2.procedure_hash != base.procedure_hash
    assert procedure_hash(base.definition) == base.procedure_hash


# ---- normalizer on real persisted-style traces -----------------------------------------------
def _real_reconstruction(
    tmp_path: Path, split: Split, task_index: int = 0
) -> tuple[RunReconstruction, LocalFsArtifactStore]:
    task = generate_split(split, DEFAULT_SEED)[task_index]
    lc, store, clock = InMemoryLifecycle(), LocalFsArtifactStore(tmp_path), FakeClock()
    result = persisted_orchestrator(clock, lc, store).execute(
        task.task, session(task), HeuristicFakeLLM(), run_tags={"dataset_split": split.value}
    )
    (run,) = lc.runs
    run = run.model_copy(update={"status": RunStatus.COMPLETED})  # type: ignore[attr-defined]
    rec = RunReconstruction(
        run=run,
        task=task.task,
        events=list(lc.events),  # type: ignore[arg-type]
        decisions=[result.decision],
        outcome=result.outcome,
    )
    return rec, store


def test_normalizer_abstracts_paths_to_roles_and_extracts_features(tmp_path: Path) -> None:
    rec, store = _real_reconstruction(tmp_path, Split.DISCOVERY)
    t = build_trajectory(rec, store.get)
    tool_tokens = t.tool_tokens
    assert tool_tokens[0] == "list_files" and tool_tokens[-1] == "set_json_value:config"
    assert {"read_file:log", "read_file:config", "read_file:infra"} <= set(tool_tokens)
    assert all("/" not in tok and ".json" not in tok for tok in tool_tokens)  # no raw paths
    assert t.file_roles == ["config", "infra", "log"] and t.config_shape
    assert t.outcome_status == STATUS.VERIFIED_SUCCESS and t.claimed_done
    assert t.tokens_used > 0 and any(s.kind == "llm" for s in t.steps)
    assert next(s for s in t.steps if s.tool == "set_json_value").value_type == "number"
    # equivalent variants share tokens even though paths and key names differ
    rec_a, store_a = _real_reconstruction(tmp_path / "a", Split.DISCOVERY, 0)
    rec_b, store_b = _real_reconstruction(tmp_path / "b", Split.DISCOVERY, 3)
    pool_a = build_trajectory(rec_a, store_a.get)
    pool_b = build_trajectory(rec_b, store_b.get)
    assert (
        pool_a.task_family == pool_b.task_family == "pool"
        and pool_a.config_shape != pool_b.config_shape
    )


def test_normalizer_refuses_runs_without_outcome_or_termination(tmp_path: Path) -> None:
    rec, store = _real_reconstruction(tmp_path, Split.DISCOVERY)
    with pytest.raises(IncompleteTraceError):
        build_trajectory(
            RunReconstruction(rec.run, rec.task, rec.events, rec.decisions, None), store.get
        )
    truncated = [e for e in rec.events if e.type.value != "RUN_TERMINATED"]
    with pytest.raises(IncompleteTraceError):
        build_trajectory(
            RunReconstruction(rec.run, rec.task, truncated, rec.decisions, rec.outcome), store.get
        )


# ---- pipeline with in-memory fakes -----------------------------------------------------------
class FakeStore:
    def __init__(self) -> None:
        self.cases: dict[tuple[str, str, str], ExperienceCase] = {}
        self.costs: list[dict[str, object]] = []
        self.artifacts: list[object] = []

    def get_case(self, tenant_id, run_id, pipeline_version):  # type: ignore[no-untyped-def]
        return self.cases.get((tenant_id, run_id, pipeline_version))

    def save_case(self, case: ExperienceCase) -> bool:
        key = (case.tenant_id, case.run_id, case.pipeline_version)
        if key in self.cases:
            return False
        self.cases[key] = case
        return True

    def register_artifact(self, tenant_id, ref, uri) -> None:  # type: ignore[no-untyped-def]
        self.artifacts.append(ref)

    def list_eligible_cases(self, tenant_id, pipeline_version):  # type: ignore[no-untyped-def]
        return [c for c in self.cases.values() if c.eligible and c.tenant_id == tenant_id]

    def record_cost(self, **kw: object) -> None:
        self.costs.append(kw)


class FakeReader:
    def __init__(self, rec: RunReconstruction | None) -> None:
        self.rec = rec

    def reconstruct(self, tenant_id: str, run_id: str) -> RunReconstruction | None:
        return self.rec


def _pipeline(tmp_path: Path, split: Split, tags: dict[str, str] | None = None):  # type: ignore[no-untyped-def]
    rec, artifacts = _real_reconstruction(tmp_path, split)
    if tags is not None:
        rec = RunReconstruction(
            rec.run.model_copy(update={"tags": tags}),
            rec.task,
            rec.events,
            rec.decisions,
            rec.outcome,
        )
    store = FakeStore()
    pipe = ExperiencePipeline(
        reader=FakeReader(rec), artifacts=artifacts, store=store, clock=FakeClock()
    )  # type: ignore[arg-type]
    return pipe, store, rec


def test_pipeline_builds_eligible_case_once_and_records_cost(tmp_path: Path) -> None:
    pipe, store, rec = _pipeline(tmp_path, Split.DISCOVERY)
    case, created = pipe.process_run(rec.run.tenant_id, rec.run.run_id)
    assert (
        created
        and case.eligible
        and case.dataset_split == DISCOVERY_SPLIT
        and case.trajectory_sha256
    )
    again, created2 = pipe.process_run(rec.run.tenant_id, rec.run.run_id)
    assert not created2 and again.case_id == case.case_id and len(store.cases) == 1
    assert store.costs and store.costs[0]["tokens"] == 0


@pytest.mark.parametrize(
    ("split", "tags", "reason"),
    [
        (Split.VALIDATION, None, ExclusionReason.NON_DISCOVERY_SPLIT),
        (Split.HIDDEN_TRANSFER, None, ExclusionReason.NON_DISCOVERY_SPLIT),
        (Split.DISCOVERY, {}, ExclusionReason.UNKNOWN_PROVENANCE),
    ],
)
def test_pipeline_excludes_non_discovery_and_unknown_provenance(
    tmp_path: Path, split, tags, reason
) -> None:  # type: ignore[no-untyped-def]
    pipe, store, rec = _pipeline(tmp_path, split, tags)
    case, _ = pipe.process_run(rec.run.tenant_id, rec.run.run_id)
    assert not case.eligible and case.excluded_reason == reason
    assert store.list_eligible_cases(rec.run.tenant_id, "experience/1") == []


def test_unreadable_artifacts_exclude_the_case_instead_of_guessing(tmp_path: Path) -> None:
    pipe, store, rec = _pipeline(tmp_path, Split.DISCOVERY)
    for f in tmp_path.rglob("*"):
        if f.is_file():
            f.unlink()
    case, _ = pipe.process_run(rec.run.tenant_id, rec.run.run_id)
    assert (
        not case.eligible
        and case.excluded_reason == ExclusionReason.INCOMPLETE_RUN
        and case.trajectory_sha256 is None
    )


def test_unknown_run_raises_not_found() -> None:
    pipe = ExperiencePipeline(
        reader=FakeReader(None), artifacts=None, store=FakeStore(), clock=FakeClock()
    )  # type: ignore[arg-type]
    with pytest.raises(RunNotFoundError):
        pipe.process_run("t", new_id())


class FakeQueue:
    def __init__(self, jobs: list[ClaimedJob]) -> None:
        self.jobs, self.done, self.retry, self.failed = jobs, [], [], []

    def claim_pending(self, job_type, batch, now):  # type: ignore[no-untyped-def]
        out, self.jobs = self.jobs[:batch], self.jobs[batch:]
        return out

    def mark_succeeded(self, tenant_id, job_id, now) -> None:  # type: ignore[no-untyped-def]
        self.done.append(job_id)

    def mark_retry_or_failed(self, tenant_id, job_id, error, give_up, now) -> None:  # type: ignore[no-untyped-def]
        (self.failed if give_up else self.retry).append(job_id)


def _job(attempts: int) -> ClaimedJob:
    from experience_engine.domain.run import LearningJob

    return ClaimedJob(
        LearningJob(
            job_id=new_id(),
            tenant_id="t",
            dedupe_key="k",
            run_id=new_id(),
            outcome_status=STATUS.VERIFIED_SUCCESS,
            eligible_for_success_mining=True,
            correlation_id="c",
            created_at=datetime.now(UTC),
        ),
        attempts,
    )


def test_job_processor_retries_boundedly_and_gives_up() -> None:
    class Boom:
        def process_run(self, tenant_id: str, run_id: str) -> None:
            raise ConnectionError("db down")

    class Missing:
        def process_run(self, tenant_id: str, run_id: str) -> None:
            raise RunNotFoundError("gone")

    queue = FakeQueue([_job(1), _job(3)])
    res = LearningJobProcessor(queue=queue, pipeline=Boom(), clock=FakeClock()).run_once()  # type: ignore[arg-type]
    assert (res.retried, res.failed) == (
        1,
        1,
    )  # attempt 1 retried; attempt 3 == max_attempts gives up
    q2 = FakeQueue([_job(1)])
    res2 = LearningJobProcessor(queue=q2, pipeline=Missing(), clock=FakeClock()).run_once()  # type: ignore[arg-type]
    assert res2.failed == 1  # a missing run can never succeed: no retry


def test_compiling_real_fake_agent_discovery_traces_yields_both_families(tmp_path: Path) -> None:
    trajectories = []
    for i, task in enumerate(generate_split(Split.DISCOVERY, DEFAULT_SEED)):
        lc, store = InMemoryLifecycle(), LocalFsArtifactStore(tmp_path / str(i))
        result = persisted_orchestrator(FakeClock(), lc, store).execute(
            task.task, session(task), HeuristicFakeLLM(), run_tags={"dataset_split": "DISCOVERY"}
        )
        rec = RunReconstruction(
            lc.runs[0].model_copy(update={"status": RunStatus.COMPLETED}),
            task.task,
            list(lc.events),
            [result.decision],
            result.outcome,
        )  # type: ignore[attr-defined,arg-type]
        trajectories.append(build_trajectory(rec, store.get))
    result_ = compile_candidates(trajectories)
    assert sorted(c.task_family for c in result_.candidates) == ["pool", "timeout"], (
        result_.rejections
    )
    for c in result_.candidates:
        assert c.definition.provenance.source_run_ids and json.loads(c.model_dump_json())
    assert RegistrationKind.NEW_CAPABILITY.value == "NEW_CAPABILITY"
    assert itertools.count  # keep import used
    assert ScriptedLLM
