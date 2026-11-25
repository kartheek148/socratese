"""ee-gov: governance of learned *memory* assets (evaluator plane; needs EE_EVALUATOR_ROLE).

  ee-gov evaluate --tenant T --root DISC --private-root P1 [--memory ID@V ...] --llm vertex
                  [--seeds 0 1] [--workers 8] [--out-dir DIR]   (default: all CANDIDATE versions)
  ee-gov observe  --tenant T --memory ID@V --mode SHADOW|CANARY --root DISC --private-root P1
  ee-gov promote  --tenant T --memory ID@V --to shadow|canary|active
  ee-gov rollback --tenant T --memory ID@V --reason TEXT
  ee-gov status   --tenant T                       (memory and capability versions with states)

Same lifecycle, same promotion policy and the same evaluation engine as capabilities (`ee-eval`);
memory is evaluated by `evaluation/memory_eval.py`. Capabilities keep being governed by `ee-eval`.
"""

import argparse
import os
import sys
from pathlib import Path

from experience_engine.adapters.artifacts.local_fs import LocalFsArtifactStore
from experience_engine.adapters.clock import SystemClock
from experience_engine.adapters.db.capability_store import SqlCapabilityStore
from experience_engine.adapters.db.engine import InfraSettings, make_engine
from experience_engine.adapters.db.memory_registry_store import SqlMemoryRegistryStore
from experience_engine.adapters.db.memory_store import SqlMemoryStore
from experience_engine.bench.dataset import DatasetLoader, EvaluatorAccess, SplitAccessError
from experience_engine.bench.verifier import SimulationVerifier
from experience_engine.capabilities.registry import PromotionBlockedError, RegistryError
from experience_engine.domain.benchmark import AgentConfig, Split
from experience_engine.domain.capability import CapabilityState
from experience_engine.domain.memory import MemoryDefinition
from experience_engine.eval_cli import _build_llm
from experience_engine.evaluation.arms import SharedArm
from experience_engine.evaluation.experiment_arms import make_baseline_arm
from experience_engine.evaluation.memory_eval import evaluate_memory, make_memory_asset_arm
from experience_engine.evaluation.policy import load_policies
from experience_engine.evaluation.report import render_evaluation
from experience_engine.execution.protocol import PROMPT_VERSION
from experience_engine.governance.memory import MemoryRegistryService

ACTOR = "evaluator:ee-gov"


class _Ctx:
    def __init__(self, policy_file: str | None) -> None:
        infra = InfraSettings.from_env()
        self.engine = make_engine(infra.database_url)
        self.artifacts = LocalFsArtifactStore(infra.artifact_root)
        self.store = SqlMemoryRegistryStore(self.engine)
        self.memories = SqlMemoryStore(self.engine)
        policies = load_policies(Path(policy_file) if policy_file else None)
        self.registry = MemoryRegistryService(self.store, policies, SystemClock(), self.artifacts)


def _parse(ref: str) -> tuple[str, int]:
    mid, _, ver = ref.partition("@")
    if not mid or not ver.isdigit():
        raise ValueError(f"memory must be given as id@version, got {ref!r}")
    return mid, int(ver)


def _loader(args: argparse.Namespace) -> DatasetLoader:
    private = args.private_root or os.environ.get("EE_PRIVATE_DATASET_ROOT")
    if not private:
        raise SplitAccessError("set --private-root or EE_PRIVATE_DATASET_ROOT")
    return DatasetLoader(Path(args.root), EvaluatorAccess.from_env(), private_root=Path(private))


def _targets(args: argparse.Namespace, ctx: _Ctx) -> list[MemoryDefinition]:
    if args.memory:
        refs = [_parse(r) for r in args.memory]
        out = []
        for mid, ver in refs:
            rec = ctx.store.get_version(args.tenant, mid, ver)
            if rec is None:
                raise RegistryError(f"{mid}@{ver} not found for tenant {args.tenant}")
            out.append(rec.definition)
        return out
    states = frozenset({CapabilityState.CANDIDATE, CapabilityState.VALIDATING})
    return [d for d, _ in ctx.memories.list_versions(args.tenant, states)]


def _cmd_evaluate(args: argparse.Namespace, ctx: _Ctx) -> int:
    loader = _loader(args)
    llm, _decisions, model = _build_llm(args.llm, 0)
    verifier, clock = SimulationVerifier(), SystemClock()
    config = AgentConfig(config_id="M_MEMORY_ASSET", prompt_version=PROMPT_VERSION, model=model)
    discovery, validation = loader.load(Split.DISCOVERY), loader.load(Split.VALIDATION)
    hidden = loader.load(Split.HIDDEN_TRANSFER)
    baseline = SharedArm(
        make_baseline_arm(
            llm=llm,
            config=AgentConfig(config_id="A_BASELINE", prompt_version=PROMPT_VERSION, model=model),
            verifier=verifier,
            clock=clock,
        )
    )
    out_dir = Path(args.out_dir) if args.out_dir else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
    targets = _targets(args, ctx)
    if not targets:
        print("no memory versions to evaluate", file=sys.stderr)
        return 2
    exit_code = 0
    for d in targets:
        rec = ctx.store.get_version(d.tenant_id, d.memory_id, d.version)
        assert rec is not None
        if rec.state == CapabilityState.CANDIDATE:
            ctx.registry.start_validation(d.tenant_id, d.memory_id, d.version, ACTOR)
        report = evaluate_memory(
            definition=d,
            policy=ctx.registry.policy_for(rec),
            clock=clock,
            baseline=baseline,
            memory_arm=make_memory_asset_arm(
                definition=d, llm=llm, config=config, verifier=verifier, clock=clock
            ),
            source_tasks=discovery.tasks,
            heldout_tasks=validation.tasks,
            transfer_tasks=hidden.tasks,
            other_family_tasks=validation.tasks,
            datasets={
                "DISCOVERY": discovery.version,
                "VALIDATION": validation.version,
                "HIDDEN_TRANSFER": hidden.version,
            },
            verifier_version=verifier.version,
            model_settings=model.model_dump(mode="json"),
            seeds=args.seeds,
            workers=args.workers,
        )
        state = ctx.registry.complete_evaluation(report, ACTOR)
        print(f"{d.memory_id}@{d.version}: verdict={report.verdict.value} state={state.value}")
        for reason in report.reasons:
            print(f"  - {reason}")
        if out_dir:
            (out_dir / f"{d.memory_id}-v{d.version}.md").write_text(render_evaluation(report))
        exit_code = max(exit_code, 1 if report.verdict.value == "FAIL" else 0)
    print(
        f"baseline runs computed={baseline.computed} reused={baseline.served - baseline.computed}"
    )
    return exit_code


def _cmd_observe(args: argparse.Namespace, ctx: _Ctx) -> int:
    loader = _loader(args)
    mid, ver = _parse(args.memory)
    rec = ctx.store.get_version(args.tenant, mid, ver)
    if rec is None:
        print("memory version not found for this tenant", file=sys.stderr)
        return 2
    llm, _d, model = _build_llm(args.llm, 0)
    verifier, clock = SimulationVerifier(), SystemClock()
    asset = make_memory_asset_arm(
        definition=rec.definition,
        llm=llm,
        config=AgentConfig(config_id="M_MEMORY_ASSET", prompt_version=PROMPT_VERSION, model=model),
        verifier=verifier,
        clock=clock,
    )
    base = make_baseline_arm(
        llm=llm,
        config=AgentConfig(config_id="A_BASELINE", prompt_version=PROMPT_VERSION, model=model),
        verifier=verifier,
        clock=clock,
    )
    family = rec.definition.task_family
    tasks = [x for x in loader.load(Split.VALIDATION).tasks if x.family == family]
    tasks = tasks[: args.max_tasks or None]
    for task in tasks:
        a, b = asset(task), base(task)
        ctx.registry.record_observation(
            rec, args.mode, task.task.task_id, a.verified, b.verified, a.tokens, b.tokens
        )
    print(f"recorded {len(tasks)} {args.mode} observations")
    return 0


def _cmd_promote(args: argparse.Namespace, ctx: _Ctx) -> int:
    mid, ver = _parse(args.memory)
    steps = {
        "shadow": ctx.registry.start_shadow,
        "canary": ctx.registry.advance_to_canary,
        "active": ctx.registry.activate,
    }
    steps[args.to](args.tenant, mid, ver, ACTOR)
    print(f"{mid}@{ver} -> {args.to.upper()}")
    return 0


def _cmd_rollback(args: argparse.Namespace, ctx: _Ctx) -> int:
    mid, ver = _parse(args.memory)
    ctx.registry.rollback(args.tenant, mid, ver, ACTOR, args.reason)
    print(f"{mid}@{ver} rolled back")
    return 0


def _cmd_status(args: argparse.Namespace, ctx: _Ctx) -> int:
    for d, state in ctx.memories.list_versions(args.tenant):
        print(f"MEMORY     {d.memory_id}@{d.version} {d.memory_type.value} {state.value}")
    for cd, state in SqlCapabilityStore(ctx.engine).list_versions(args.tenant):
        print(f"CAPABILITY {cd.id}@{cd.version} {state.value}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ee-gov")
    parser.add_argument("--policy-file", default=os.environ.get("EE_PROMOTION_POLICY_FILE"))
    sub = parser.add_subparsers(dest="command", required=True)

    def tenant(p: argparse.ArgumentParser) -> None:
        p.add_argument("--tenant", required=True)

    ev = sub.add_parser("evaluate")
    tenant(ev)
    ev.add_argument("--memory", nargs="+", metavar="ID@VERSION")
    ev.add_argument("--llm", choices=["fake", "vertex"], default="vertex")
    ev.add_argument("--root", required=True)
    ev.add_argument("--private-root", default=None)
    ev.add_argument("--seeds", type=int, nargs="+", default=[0, 1])
    ev.add_argument("--workers", type=int, default=8)
    ev.add_argument("--out-dir", default=None)
    ev.set_defaults(func=_cmd_evaluate)

    ob = sub.add_parser("observe")
    tenant(ob)
    ob.add_argument("--memory", required=True, metavar="ID@VERSION")
    ob.add_argument("--mode", choices=["SHADOW", "CANARY"], required=True)
    ob.add_argument("--llm", choices=["fake", "vertex"], default="vertex")
    ob.add_argument("--root", required=True)
    ob.add_argument("--private-root", default=None)
    ob.add_argument("--max-tasks", type=int, default=0)
    ob.set_defaults(func=_cmd_observe)

    pr = sub.add_parser("promote")
    tenant(pr)
    pr.add_argument("--memory", required=True, metavar="ID@VERSION")
    pr.add_argument("--to", choices=["shadow", "canary", "active"], required=True)
    pr.set_defaults(func=_cmd_promote)

    rb = sub.add_parser("rollback")
    tenant(rb)
    rb.add_argument("--memory", required=True, metavar="ID@VERSION")
    rb.add_argument("--reason", required=True)
    rb.set_defaults(func=_cmd_rollback)

    st = sub.add_parser("status")
    tenant(st)
    st.set_defaults(func=_cmd_status)

    args = parser.parse_args(argv)
    try:
        EvaluatorAccess.from_env()
    except SplitAccessError as exc:
        print(str(exc), file=sys.stderr)
        return 3
    try:
        return int(args.func(args, _Ctx(args.policy_file)))
    except (RegistryError, PromotionBlockedError, ValueError, SplitAccessError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
