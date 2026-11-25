"""ee-exp: the capability-vs-memory experiment. Evaluator-plane tool (needs EE_EVALUATOR_ROLE).

  ee-exp run    --tenant T --root DIR --private-root DIR --capabilities ID@V [ID@V ...]
                [--llm vertex|fake] [--model NAME] [--reps 2] [--workers 8] [--arms A B C D E]
                [--max-tasks N] --out results.json [--report report.md]
  ee-exp report --results results.json --out report.md [--title T] [--maintenance-tokens N]

Arms B/C/D/E are all built from the same eligible discovery trajectories in the database.
Nothing here writes to the registry, so promotion state is never changed by an experiment.
"""

import argparse
import hashlib
import os
import sys
from collections import defaultdict
from collections.abc import Callable, Sequence
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import Engine

from experience_engine import RUNTIME_VERSION
from experience_engine.adapters.artifacts.local_fs import LocalFsArtifactStore
from experience_engine.adapters.clock import SystemClock
from experience_engine.adapters.db import tables as t
from experience_engine.adapters.db.capability_store import SqlCapabilityStore
from experience_engine.adapters.db.catalog import SqlExecutableCatalog
from experience_engine.adapters.db.engine import InfraSettings, make_engine
from experience_engine.adapters.db.experience_store import SqlExperienceStore
from experience_engine.adapters.db.memory_catalog import SqlActiveMemoryCatalog, StaticMemoryCatalog
from experience_engine.adapters.db.registry_store import SqlRegistryStore
from experience_engine.bench.dataset import DatasetLoader, EvaluatorAccess
from experience_engine.bench.environment import ENVIRONMENT_VERSION
from experience_engine.bench.verifier import SimulationVerifier
from experience_engine.domain.benchmark import AgentConfig, DatasetVersion, ModelSettings, Split
from experience_engine.domain.capability import (
    EXECUTABLE_STATES,
    CapabilityDefinition,
    CapabilityState,
    TaskFamilyIs,
)
from experience_engine.domain.experience import Trajectory
from experience_engine.domain.experiment import (
    ARMS,
    GATED_ARMS,
    LEGACY_ARMS,
    ExperimentConfig,
    ExperimentResult,
    LearningLedger,
)
from experience_engine.domain.memory import MemoryType
from experience_engine.eval_cli import _build_llm
from experience_engine.evaluation.arms import ArmRunner
from experience_engine.evaluation.engine import EVALUATOR_VERSION
from experience_engine.evaluation.experiment import (
    ExperimentTask,
    applicability_matrix,
    run_experiment,
    tag_tasks,
)
from experience_engine.evaluation.experiment_arms import (
    make_baseline_arm,
    make_gated_memory_arm,
    make_memory_arm,
    make_runtime_arm,
)
from experience_engine.evaluation.experiment_report import render_experiment
from experience_engine.execution.capability_runtime import (
    CapabilityRuntime,
    StaticCapabilityCatalog,
)
from experience_engine.execution.matcher import CapabilityMatcher
from experience_engine.execution.protocol import PROMPT_VERSION
from experience_engine.memory.bank import MAX_EPISODES, ExperienceMemory
from experience_engine.memory.builders import PREAMBLE
from experience_engine.memory.service import load_experience
from experience_engine.ports.artifacts import ArtifactStore
from experience_engine.ports.experience import ExecutableCapabilityCatalog


def load_discovery_experience(
    store: SqlExperienceStore, artifacts: ArtifactStore, tenant: str
) -> list[Trajectory]:
    return load_experience(store, artifacts, tenant)[0]


def _family_of(d: CapabilityDefinition) -> str:
    return next((p.family for p in d.preconditions if isinstance(p, TaskFamilyIs)), "?")


def build_ledger(
    engine: Engine,
    tenant: str,
    trajectories: Sequence[Trajectory],
    definitions: Sequence[CapabilityDefinition],
    consolidation_ms: int,
) -> tuple[LearningLedger, dict[str, LearningLedger]]:
    """Learning cost from recorded data: discovery tokens from the trajectories, analysis and
    compilation from `cost_ledger`, validation from stored evaluation records."""
    c = t.cost_ledger
    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(
                c.c.subject_type,
                sa.func.coalesce(sa.func.sum(c.c.tokens), 0),
                sa.func.coalesce(sa.func.sum(c.c.wall_ms), 0),
            )
            .where(c.c.tenant_id == tenant)
            .group_by(c.c.subject_type)
        ).all()
        costs = {r[0]: (int(r[1]), int(r[2])) for r in rows}
        ev = t.capability_evaluations
        eval_rows = conn.execute(
            sa.select(ev.c.capability_id, ev.c.version, ev.c.metrics, ev.c.completed_at)
            .where(ev.c.tenant_id == tenant)
            .order_by(ev.c.completed_at)
        ).all()
    latest = {(r[0], r[1]): r[2] for r in eval_rows}  # newest evaluation per version
    wanted = {(d.id, d.version): _family_of(d) for d in definitions}
    notes: list[str] = []
    validation: dict[str, int] = defaultdict(int)
    for key, fam in wanted.items():
        metrics = latest.get(key)
        if metrics is None:
            notes.append(f"no evaluation record for {key[0]}@{key[1]}: its validation cost is 0")
            continue
        life = metrics["lifecycle"]
        validation[fam] += int(life["evaluation_overhead_tokens"]) + int(
            life["capability_exec_tokens"]
        )
    analysis_tokens, analysis_ms = costs.get("run", (0, 0))
    compile_tokens, compile_ms = costs.get("compile", (0, 0))

    def ledger(trajs: Sequence[Trajectory], families: set[str] | None) -> LearningLedger:
        return LearningLedger(
            discovery_tokens=sum(x.tokens_used for x in trajs),
            discovery_tool_calls=sum(x.tool_calls for x in trajs),
            discovery_runs=len(trajs),
            analysis_tokens=analysis_tokens,
            consolidation_tokens=0,
            compilation_tokens=compile_tokens,
            validation_tokens=sum(
                v for f, v in validation.items() if families is None or f in families
            ),
            wall_ms_analysis=analysis_ms,
            wall_ms_consolidation=consolidation_ms,
            wall_ms_compilation=compile_ms,
            notes=notes
            + [
                "analysis/compilation/consolidation are deterministic (0 model tokens); only wall "
                "time is charged",
                "discovery cost is the model tokens of the discovery runs that every learning arm "
                "reuses",
            ],
        )

    total = ledger(trajectories, None)
    by_family = {
        f: ledger([x for x in trajectories if (x.task_family or "unknown") == f], {f})
        for f in sorted({x.task_family or "unknown" for x in trajectories})
    }
    return total, by_family


def _parse_ref(ref: str) -> tuple[str, int]:
    cid, _, ver = ref.partition("@")
    if not cid or not ver.isdigit():
        raise ValueError(f"capability must be given as id@version, got {ref!r}")
    return cid, int(ver)


_ARM_OF_TYPE = {"EPISODIC": "B", "CONSOLIDATED": "C", "PROCEDURAL": "D"}


def build_gated_ledger(
    engine: Engine,
    tenant: str,
    trajectories: Sequence[Trajectory],
) -> tuple[LearningLedger, dict[str, LearningLedger]]:
    """Gated-mode ledger. Validation cost is what the stored evaluations of each arm's asset type
    actually consumed (EVERY evaluation, rejected assets included: that money was spent). Memory
    construction is deterministic (0 model tokens) and only its wall time is charged."""
    c = t.cost_ledger
    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(
                c.c.subject_type,
                sa.func.coalesce(sa.func.sum(c.c.tokens), 0),
                sa.func.coalesce(sa.func.sum(c.c.wall_ms), 0),
            )
            .where(c.c.tenant_id == tenant)
            .group_by(c.c.subject_type)
        ).all()
        costs = {r[0]: (int(r[1]), int(r[2])) for r in rows}
        mem = conn.execute(
            sa.select(
                t.memory_evaluations.c.memory_id,
                t.memory_evaluations.c.metrics,
                t.memories.c.memory_type,
                t.memories.c.task_family,
            )
            .select_from(
                t.memory_evaluations.join(
                    t.memories,
                    sa.and_(
                        t.memories.c.tenant_id == t.memory_evaluations.c.tenant_id,
                        t.memories.c.memory_id == t.memory_evaluations.c.memory_id,
                    ),
                )
            )
            .where(t.memory_evaluations.c.tenant_id == tenant)
        ).all()
        cap = conn.execute(
            sa.select(t.capability_evaluations.c.metrics, t.capabilities.c.task_family)
            .select_from(
                t.capability_evaluations.join(
                    t.capabilities,
                    sa.and_(
                        t.capabilities.c.tenant_id == t.capability_evaluations.c.tenant_id,
                        t.capabilities.c.capability_id == t.capability_evaluations.c.capability_id,
                    ),
                )
            )
            .where(t.capability_evaluations.c.tenant_id == tenant)
        ).all()

    def spent(metrics: dict[str, object]) -> int:
        life = metrics["lifecycle"]
        assert isinstance(life, dict)
        return int(life["evaluation_overhead_tokens"]) + int(life["capability_exec_tokens"])

    by_arm_family: dict[tuple[str, str], list[int]] = defaultdict(list)
    for _mid, metrics, mtype, fam in mem:
        by_arm_family[(_ARM_OF_TYPE[mtype], fam)].append(spent(metrics))
    for metrics, fam in cap:
        by_arm_family[("E", fam)].append(spent(metrics))
    analysis_tokens, analysis_ms = costs.get("run", (0, 0))
    compile_tokens, compile_ms = costs.get("compile", (0, 0))
    build_tokens, build_ms = costs.get("memory_build", (0, 0))

    def ledger(trajs: Sequence[Trajectory], family: str | None) -> LearningLedger:
        by_arm: dict[str, int] = {a: 0 for a in "BCDE"}
        runs: dict[str, int] = {a: 0 for a in "BCDE"}
        for (arm, fam), vals in by_arm_family.items():
            if family is None or fam == family:
                by_arm[arm] += sum(vals)
                runs[arm] += len(vals)
        return LearningLedger(
            discovery_tokens=sum(x.tokens_used for x in trajs),
            discovery_tool_calls=sum(x.tool_calls for x in trajs),
            discovery_runs=len(trajs),
            analysis_tokens=analysis_tokens,
            consolidation_tokens=build_tokens,
            compilation_tokens=compile_tokens,
            validation_tokens=sum(by_arm.values()),
            wall_ms_analysis=analysis_ms,
            wall_ms_compilation=compile_ms,
            wall_ms_memory_build=build_ms,
            validation_by_arm=by_arm,
            validation_runs_by_arm=runs,
            notes=[
                "analysis, memory construction and compilation are deterministic (0 model "
                "tokens); only wall time is charged",
                "discovery cost is the model tokens of the discovery runs that every learning arm "
                "reuses",
                "validation tokens are summed over every stored evaluation of the arm's asset "
                "type, rejected assets included; the shared baseline was run once per session but "
                "each asset is charged the control tokens its own evaluation consumed",
            ],
        )

    total = ledger(trajectories, None)
    by_family = {
        f: ledger([x for x in trajectories if (x.task_family or "unknown") == f], f)
        for f in sorted({x.task_family or "unknown" for x in trajectories})
    }
    return total, by_family


def _cmd_run(args: argparse.Namespace) -> int:
    access = EvaluatorAccess.from_env()
    gated = args.mode == "gated"
    private = args.private_root or os.environ.get("EE_PRIVATE_DATASET_ROOT")
    if not private:
        print("set --private-root or EE_PRIVATE_DATASET_ROOT (fresh private data)", file=sys.stderr)
        return 2
    if not gated and not args.capabilities:
        print("legacy mode needs --capabilities ID@VERSION ...", file=sys.stderr)
        return 2
    if args.model:
        os.environ["EE_LLM_MODEL"] = args.model
    infra = InfraSettings.from_env()
    engine = make_engine(infra.database_url)
    artifacts = LocalFsArtifactStore(infra.artifact_root)
    registry = SqlRegistryStore(engine)

    definitions: list[CapabilityDefinition] = []
    states: list[str] = []
    asset_states = (
        {CapabilityState(x.strip().upper()) for x in args.experimental_states}
        if args.experimental_states
        else set(EXECUTABLE_STATES)
    )
    if gated:
        catalog: ExecutableCapabilityCatalog = SqlExecutableCatalog(engine, asset_states)
        definitions = catalog.list_executable(args.tenant)
        states = sorted(s.value for s in asset_states)
    else:
        for ref in args.capabilities:
            cid, ver = _parse_ref(ref)
            rec = registry.get_version(args.tenant, cid, ver)
            if rec is None:
                print(f"capability {ref} not found for tenant {args.tenant}", file=sys.stderr)
                return 2
            definitions.append(rec.definition)
            states.append(rec.state.value)
        catalog = StaticCapabilityCatalog(args.tenant, definitions)

    trajectories = load_discovery_experience(SqlExperienceStore(engine), artifacts, args.tenant)
    if not trajectories:
        print("no eligible discovery experience (run ee-learn first)", file=sys.stderr)
        return 2
    # ungated memory (legacy behaviour) is built from ALL known procedures, whatever their state
    all_caps = [d for d, _ in SqlCapabilityStore(engine).list_versions(args.tenant)]
    memory = ExperienceMemory(trajectories, all_caps if gated else definitions)
    learned = {x.task_family or "unknown" for x in trajectories}

    loader = DatasetLoader(Path(args.root), access, private_root=Path(private))
    validation, hidden = loader.load(Split.VALIDATION), loader.load(Split.HIDDEN_TRANSFER)
    tasks: list[ExperimentTask] = [
        *tag_tasks("VALIDATION", validation.tasks, learned),
        *tag_tasks("HIDDEN_TRANSFER", hidden.tasks, learned),
    ]
    if args.max_tasks:
        tasks = tasks[: args.max_tasks]

    llm, decisions, model = _build_llm(args.llm, 0)
    verifier, clock = SimulationVerifier(), SystemClock()

    def cfg(config_id: str) -> AgentConfig:
        return AgentConfig(config_id=config_id, prompt_version=PROMPT_VERSION, model=model)

    runtime = CapabilityRuntime(
        catalog=catalog,
        matcher=CapabilityMatcher(catalog),
        decisions_for=lambda _llm: decisions,
        experimental=sorted(set(states)) != ["ACTIVE"],
    )
    builders: dict[str, ArmRunner] = {
        "A": make_baseline_arm(llm=llm, config=cfg("A_BASELINE"), verifier=verifier, clock=clock),
        "E": make_runtime_arm(
            llm=llm,
            config=cfg("E_COMPILED"),
            verifier=verifier,
            clock=clock,
            runtime=runtime,
            tenant_id=args.tenant,
        ),
    }
    memory_assets: list[str] = []
    arm_scope: dict[str, Callable[[ExperimentTask], bool]] = {}
    ungated = set(args.ungated_families or [])
    if gated:
        mem_catalog = SqlActiveMemoryCatalog(engine, asset_states)
        memory_assets = [f"{d.memory_id}@{d.version}" for d in mem_catalog.list_active(args.tenant)]
        for arm, mtype in (
            ("B", MemoryType.EPISODIC),
            ("C", MemoryType.CONSOLIDATED),
            ("D", MemoryType.PROCEDURAL),
        ):
            builders[arm] = make_gated_memory_arm(
                llm=llm,
                config=cfg(f"{arm}_GATED_{mtype.value}"),
                verifier=verifier,
                clock=clock,
                catalog=mem_catalog,
                tenant_id=args.tenant,
                memory_type=mtype,
            )
        builders["F"] = make_gated_memory_arm(  # noise control: the same path, empty registry
            llm=llm,
            config=cfg("F_NOISE_CONTROL"),
            verifier=verifier,
            clock=clock,
            catalog=StaticMemoryCatalog(args.tenant, []),
            tenant_id=args.tenant,
            memory_type=MemoryType.EPISODIC,
        )
        for arm, retrieve in (
            ("UB", memory.episodic),
            ("UC", memory.consolidated),
            ("UD", memory.instructions),
        ):
            builders[arm] = make_memory_arm(
                llm=llm,
                config=cfg(f"{arm}_UNGATED"),
                verifier=verifier,
                clock=clock,
                retrieve=retrieve,
            )
            arm_scope[arm] = lambda et: et.bench.family in ungated
    else:
        for arm, retrieve in (
            ("B", memory.episodic),
            ("C", memory.consolidated),
            ("D", memory.instructions),
        ):
            builders[arm] = make_memory_arm(
                llm=llm,
                config=cfg(f"{arm}_LEGACY"),
                verifier=verifier,
                clock=clock,
                retrieve=retrieve,
            )
    default_arms = list(GATED_ARMS if gated else LEGACY_ARMS) + (
        ["UB", "UC", "UD"] if gated and ungated else []
    )
    wanted = args.arms or default_arms
    arms = {a: builders[a] for a in ARMS if a in wanted and a in builders}
    rows = run_experiment(
        arms, tasks, repetitions=args.reps, workers=args.workers, arm_scope=arm_scope
    )
    # read after the grid so validation runs (ee-eval / ee-gov) that finish meanwhile are included
    if gated:
        ledger, ledger_by_family = build_gated_ledger(engine, args.tenant, trajectories)
    else:
        ledger, ledger_by_family = build_ledger(
            engine, args.tenant, trajectories, definitions, memory.consolidation_ms
        )

    config = ExperimentConfig(
        name=args.name,
        model=_model_dict(model),
        arms=list(arms),
        repetitions=args.reps,
        workers=args.workers,
        datasets={
            "VALIDATION": _version(validation.version),
            "HIDDEN_TRANSFER": _version(hidden.version),
        },
        discovery_trajectories=len(trajectories),
        discovery_families=sorted(learned),
        capabilities=[f"{d.id}@{d.version}" for d in definitions],
        capability_catalog_states=sorted(set(states)),
        memory_preamble_sha256=hashlib.sha256(PREAMBLE.encode()).hexdigest(),
        max_episodes=MAX_EPISODES,
        verifier_version=verifier.version,
        runtime_version=RUNTIME_VERSION,
        evaluator_version=EVALUATOR_VERSION,
        mode=args.mode,
        memory_assets=memory_assets,
        asset_states=sorted(s.value for s in asset_states),
        ungated_families=sorted(ungated),
        notes=[f"environment {ENVIRONMENT_VERSION}"],
    )
    result = ExperimentResult(
        config=config,
        ledger=ledger,
        ledger_by_family=ledger_by_family,
        rows=rows,
        applicability=applicability_matrix(definitions, tasks),
    )
    Path(args.out).write_text(result.model_dump_json(indent=1))
    n_unknown = sum(1 for r in rows if r.status == "UNKNOWN")
    print(f"wrote {args.out}: {len(rows)} runs ({n_unknown} UNKNOWN) over {len(tasks)} tasks")
    if args.report:
        Path(args.report).write_text(render_experiment(result, title=args.name))
        print(f"wrote {args.report}")
    return 0


def _version(v: DatasetVersion) -> dict[str, str]:
    return {"name": v.name, "version": v.version, "content_hash": v.content_hash}


def _model_dict(model: ModelSettings) -> dict[str, object]:
    d = model.model_dump(mode="json")
    d.pop("seed", None)  # the provider ignores seeds; recording one would suggest determinism
    return d


def _cmd_report(args: argparse.Namespace) -> int:
    result = ExperimentResult.model_validate_json(Path(args.results).read_text())
    text = render_experiment(result, title=args.title, maintenance_tokens=args.maintenance_tokens)
    Path(args.out).write_text(text)
    print(f"wrote {args.out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ee-exp")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run arms A-E on identical tasks and repetitions")
    run.add_argument("--tenant", default="bench")
    run.add_argument("--root", required=True, help="public dataset root (manifest/discovery)")
    run.add_argument("--private-root", default=None)
    run.add_argument(
        "--mode",
        choices=["legacy", "gated"],
        default="legacy",
        help="legacy: explicit capabilities + ungated memory; gated: arms B-E read only ACTIVE "
        "assets from the registry and fall back to planning with a recorded reason",
    )
    run.add_argument("--capabilities", nargs="+", default=[], metavar="ID@VERSION")
    run.add_argument(
        "--experimental-states",
        nargs="+",
        metavar="STATE",
        help="gated mode, EXPERIMENT ONLY: also read assets in these states; the report is tagged",
    )
    run.add_argument(
        "--ungated-families",
        nargs="+",
        default=[],
        help="gated mode: also run the legacy always-inject memory arms (UB/UC/UD) on these "
        "families, to measure negative transfer that gating prevents",
    )
    run.add_argument("--llm", choices=["fake", "vertex"], default="vertex")
    run.add_argument("--model", default=None, help="model name override (EE_LLM_MODEL)")
    run.add_argument("--reps", type=int, default=2)
    run.add_argument("--workers", type=int, default=8)
    run.add_argument("--arms", nargs="+", choices=list(ARMS), default=None)
    run.add_argument("--max-tasks", type=int, default=0)
    run.add_argument("--name", default="capability-vs-memory experiment")
    run.add_argument("--out", required=True)
    run.add_argument("--report", default=None)
    run.set_defaults(func=_cmd_run)
    rep = sub.add_parser("report", help="render a markdown report from a results file")
    rep.add_argument("--results", required=True)
    rep.add_argument("--out", required=True)
    rep.add_argument("--title", default="Capability vs memory experiment")
    rep.add_argument("--maintenance-tokens", type=int, default=0)
    rep.set_defaults(func=_cmd_report)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
