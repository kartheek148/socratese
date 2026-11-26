"""ee-eval: evaluator-plane commands. Every command requires evaluator access
(EE_EVALUATOR_ROLE=evaluator) - the opposite of ee-learn, which refuses it.

  ee-eval evaluate --tenant T --capability ID --version N [--llm fake|vertex] [--private-root DIR]
  ee-eval evaluate ... [--seeds 0 1 2] [--workers 8]
  ee-eval observe  --tenant T --capability ID --version N --mode SHADOW|CANARY [--max-tasks K]
  ee-eval promote  --tenant T --capability ID --version N --to shadow|canary|active
  ee-eval rollback --tenant T --capability ID --version N --reason TEXT
  ee-eval status   --tenant T
"""

import argparse
import os
import sys
from pathlib import Path

import sqlalchemy as sa

from experience_engine.adapters.artifacts.local_fs import LocalFsArtifactStore
from experience_engine.adapters.clock import SystemClock
from experience_engine.adapters.db import tables as t
from experience_engine.adapters.db.capability_store import SqlCapabilityStore
from experience_engine.adapters.db.engine import InfraSettings, make_engine
from experience_engine.adapters.db.experience_store import SqlExperienceStore
from experience_engine.adapters.db.registry_store import SqlRegistryStore
from experience_engine.adapters.llm.decisions import LlmDecisionProvider
from experience_engine.adapters.llm.fake import HeuristicDecisionProvider, HeuristicFakeLLM
from experience_engine.adapters.llm.openai_compatible import OpenAICompatibleClient
from experience_engine.adapters.llm.vertex import (
    service_account_token_provider,
    vertex_config_from_env,
)
from experience_engine.bench.dataset import (
    DatasetLoader,
    EvaluatorAccess,
    SplitAccessError,
)
from experience_engine.bench.verifier import SimulationVerifier
from experience_engine.capabilities.registry import (
    PromotionBlockedError,
    RegistryError,
    RegistryService,
)
from experience_engine.domain.benchmark import AgentConfig, ModelSettings, Split
from experience_engine.domain.capability import CapabilityState
from experience_engine.domain.evaluation import Arm
from experience_engine.domain.experience import PIPELINE_VERSION, Trajectory
from experience_engine.evaluation.arms import (
    ArmRunner,
    SharedArm,
    episodic_hint,
    make_agent_arm,
    make_capability_arm,
)
from experience_engine.evaluation.baseline_cache import make_baseline_store
from experience_engine.evaluation.engine import EvaluationPlan, evaluate
from experience_engine.evaluation.policy import POLICY_SETS, builtin_policies, load_policies
from experience_engine.evaluation.report import render_evaluation
from experience_engine.evaluation.sequential import StagePlan
from experience_engine.evaluation.validation_cost import format_resources
from experience_engine.execution.protocol import PROMPT_VERSION
from experience_engine.ports.decisions import DecisionProvider
from experience_engine.ports.llm import LLMClient

ACTOR = "evaluator:ee-eval"


def _build_llm(kind: str, seed: int) -> tuple[LLMClient, DecisionProvider, ModelSettings]:
    if kind == "fake":
        model = ModelSettings(provider="fake", model="heuristic-fake/1", seed=seed)
        return HeuristicFakeLLM(), HeuristicDecisionProvider(), model
    vertex = vertex_config_from_env(dict(os.environ))
    settings = vertex.settings(
        temperature=float(os.environ.get("EE_LLM_TEMPERATURE", "0")),
        max_output_tokens=int(os.environ.get("EE_LLM_MAX_OUTPUT_TOKENS", "0")) or None,
    )
    client = OpenAICompatibleClient(
        settings, token_provider=service_account_token_provider(vertex.credentials_file)
    )
    model = ModelSettings(
        provider=vertex.provider_label,
        model=settings.model,
        temperature=settings.temperature,
        seed=seed,
        max_output_tokens=settings.max_output_tokens,
    )
    return client, LlmDecisionProvider(client, model), model


def _loader(args: argparse.Namespace) -> DatasetLoader:
    private = args.private_root or os.environ.get("EE_PRIVATE_DATASET_ROOT")
    if not private:
        raise SplitAccessError(
            "evaluation requires private validation/hidden data: set --private-root or "
            "EE_PRIVATE_DATASET_ROOT (see docs/benchmark.md)"
        )
    return DatasetLoader(Path(args.root), EvaluatorAccess.from_env(), private_root=Path(private))


def _cmd_evaluate(args: argparse.Namespace, ctx: "_Ctx") -> int:
    loader = _loader(args)
    rec = ctx.store.get_version(args.tenant, args.capability, args.version)
    if rec is None:
        print("capability version not found for this tenant", file=sys.stderr)
        return 2
    if rec.state == CapabilityState.CANDIDATE:
        ctx.registry.start_validation(args.tenant, args.capability, args.version, ACTOR)
    llm, decisions, model = _build_llm(args.llm, args.seed)
    verifier = SimulationVerifier()
    clock = SystemClock()
    discovery, validation = loader.load(Split.DISCOVERY), loader.load(Split.VALIDATION)
    hidden = loader.load(Split.HIDDEN_TRANSFER)
    limit = args.max_tasks or None

    hint = _episodic_hint_for(ctx, args.tenant, rec.definition.preconditions)
    config = AgentConfig(prompt_version=PROMPT_VERSION, model=model)
    baseline: ArmRunner = make_agent_arm(llm=llm, config=config, verifier=verifier, clock=clock)
    store = (
        make_baseline_store(
            Path(args.baseline_cache),
            model_settings=model.model_dump(mode="json"),
            verifier_version=verifier.version,
        )
        if args.baseline_cache
        else None
    )
    if store is not None:
        baseline = SharedArm(baseline, store)
    arms: dict[Arm, ArmRunner] = {
        Arm.BASELINE: baseline,
        Arm.MEMORY: make_agent_arm(
            llm=llm,
            config=config,
            verifier=verifier,
            clock=clock,
            hint_for=(lambda _t: hint) if hint else None,
        ),
        Arm.CAPABILITY: make_capability_arm(
            definition=rec.definition,
            tenant_id=args.tenant,
            decisions=decisions,
            verifier=verifier,
            clock=clock,
        ),
    }
    if args.skip_memory_control:
        del arms[Arm.MEMORY]  # the B_MEMORY control feeds no gate; only the overhead figure
    cf = (
        [builtin_policies(args.counterfactual_policy_set)[ctx.registry.risk_class(rec)]]
        if args.counterfactual_policy_set
        else []
    )
    tokens, wall = _learning_cost(ctx, args.tenant, rec.definition.provenance.source_run_ids)
    plan = EvaluationPlan(
        tenant_id=args.tenant,
        definition=rec.definition,
        policy=ctx.registry.policy_for(rec),
        clock=clock,
        arms=arms,
        source_tasks=discovery.tasks[:limit] if limit else discovery.tasks,
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
        seeds=args.seeds or [args.seed],
        workers=args.workers,
        learning_tokens=tokens,
        learning_wall_ms=wall,
        counterfactual_policies=list(cf),
        stages=StagePlan() if args.staged else None,
    )
    if limit:
        plan.heldout_tasks = plan.heldout_tasks[: limit * 4]
    report = evaluate(plan)
    new_state = ctx.registry.complete_evaluation(report, ACTOR)
    text = render_evaluation(report)
    if args.out:
        Path(args.out).write_text(text)
    print(f"verdict={report.verdict.value} state={new_state.value}")
    for reason in report.reasons:
        print(f"  - {reason}")
    for c in report.counterfactuals:
        print(f"counterfactual {c.policy_name}: {c.verdict.value} (not acted on)")
    if report.resources:
        for line in format_resources(report.resources):
            print(f"resources {line}")
    if store is not None:
        print(f"baseline cache: hits={store.hits} misses={store.misses} puts={store.puts}")
    return 0 if report.verdict.value != "FAIL" else 1


def _episodic_hint_for(ctx: "_Ctx", tenant: str, preconditions: list) -> str | None:  # type: ignore[type-arg]
    family = next((p.family for p in preconditions if p.kind == "task_family_is"), None)
    if family is None:
        return None
    for case in ctx.experience.list_eligible_cases(tenant, PIPELINE_VERSION):
        if (
            case.task_family == family
            and case.outcome_status.value == "VERIFIED_SUCCESS"
            and case.trajectory_sha256
        ):
            raw = ctx.artifacts.get(tenant, case.trajectory_sha256)
            return episodic_hint(Trajectory.model_validate_json(raw))
    return None


def _learning_cost(ctx: "_Ctx", tenant: str, run_ids: list[str]) -> tuple[int, int]:
    c = t.cost_ledger
    with ctx.engine.connect() as conn:
        rows = conn.execute(
            sa.select(
                sa.func.coalesce(sa.func.sum(c.c.tokens), 0),
                sa.func.coalesce(sa.func.sum(c.c.wall_ms), 0),
            ).where(
                c.c.tenant_id == tenant,
                sa.or_(c.c.subject_id.in_(run_ids), c.c.subject_type == "compile"),
            )
        ).one()
    return int(rows[0]), int(rows[1])


def _cmd_promote(args: argparse.Namespace, ctx: "_Ctx") -> int:
    steps = {
        "shadow": ctx.registry.start_shadow,
        "canary": ctx.registry.advance_to_canary,
        "active": ctx.registry.activate,
    }
    steps[args.to](args.tenant, args.capability, args.version, ACTOR)
    print(f"{args.capability} v{args.version} -> {args.to.upper()}")
    return 0


def _cmd_rollback(args: argparse.Namespace, ctx: "_Ctx") -> int:
    ctx.registry.rollback(args.tenant, args.capability, args.version, ACTOR, args.reason)
    print(f"{args.capability} v{args.version} rolled back")
    return 0


def _cmd_observe(args: argparse.Namespace, ctx: "_Ctx") -> int:
    loader = _loader(args)
    rec = ctx.store.get_version(args.tenant, args.capability, args.version)
    if rec is None:
        print("capability version not found for this tenant", file=sys.stderr)
        return 2
    llm, decisions, model = _build_llm(args.llm, args.seed)
    verifier, clock = SimulationVerifier(), SystemClock()
    config = AgentConfig(prompt_version=PROMPT_VERSION, model=model)
    baseline = make_agent_arm(llm=llm, config=config, verifier=verifier, clock=clock)
    cap = make_capability_arm(
        definition=rec.definition,
        tenant_id=args.tenant,
        decisions=decisions,
        verifier=verifier,
        clock=clock,
    )
    family = next(
        (p.family for p in rec.definition.preconditions if p.kind == "task_family_is"), ""
    )
    tasks = [x for x in loader.load(Split.VALIDATION).tasks if x.family == family][
        : args.max_tasks or None
    ]
    for task in tasks:
        c, b = cap(task), baseline(task)
        ctx.registry.record_observation(
            rec, args.mode, task.task.task_id, c.verified, b.verified, c.tokens, b.tokens
        )
    print(f"recorded {len(tasks)} {args.mode} observations")
    return 0


def _cmd_status(args: argparse.Namespace, ctx: "_Ctx") -> int:
    for definition, state in ctx.capabilities.list_versions(args.tenant):
        print(f"{definition.id} v{definition.version} {state.value}")
    return 0


class _Ctx:
    def __init__(self, policy_file: str | None, policy_set: str = "default") -> None:
        infra = InfraSettings.from_env()
        self.engine = make_engine(infra.database_url)
        self.artifacts = LocalFsArtifactStore(infra.artifact_root)
        self.store = SqlRegistryStore(self.engine)
        self.capabilities = SqlCapabilityStore(self.engine)
        self.experience = SqlExperienceStore(self.engine)
        policies = load_policies(Path(policy_file) if policy_file else None, policy_set)
        self.registry = RegistryService(self.store, policies, SystemClock(), self.artifacts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ee-eval")
    parser.add_argument("--policy-file", default=os.environ.get("EE_PROMOTION_POLICY_FILE"))
    parser.add_argument(
        "--policy-set",
        choices=POLICY_SETS,
        default="default",
        help="built-in promotion policies: default (count-based) or risk-v2 (candidate)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser, version: bool = True) -> None:
        p.add_argument("--tenant", required=True)
        if version:
            p.add_argument("--capability", required=True)
            p.add_argument("--version", type=int, required=True)

    ev = sub.add_parser("evaluate")
    common(ev)
    ev.add_argument("--llm", choices=["fake", "vertex"], default="fake")
    ev.add_argument("--root", default="benchmarks/v1")
    ev.add_argument("--private-root", default=None)
    ev.add_argument("--seed", type=int, default=0)
    ev.add_argument("--max-tasks", type=int, default=0)
    ev.add_argument(
        "--seeds",
        type=int,
        nargs="+",
        help="run every task once per seed (independent repetitions); default: --seed only",
    )
    ev.add_argument("--workers", type=int, default=1, help="concurrent task runs")
    ev.add_argument("--out", default=None)
    ev.add_argument("--baseline-cache", default=None, help="directory of reusable baseline runs")
    ev.add_argument("--staged", action="store_true", help="predefined sequential stopping rules")
    ev.add_argument("--skip-memory-control", action="store_true")
    ev.add_argument(
        "--counterfactual-policy-set",
        choices=POLICY_SETS,
        default=None,
        help="also record what this policy set would decide (never acted on)",
    )
    ev.set_defaults(func=_cmd_evaluate)

    ob = sub.add_parser("observe")
    common(ob)
    ob.add_argument("--mode", choices=["SHADOW", "CANARY"], required=True)
    ob.add_argument("--llm", choices=["fake", "vertex"], default="fake")
    ob.add_argument("--root", default="benchmarks/v1")
    ob.add_argument("--private-root", default=None)
    ob.add_argument("--seed", type=int, default=0)
    ob.add_argument("--max-tasks", type=int, default=0)
    ob.set_defaults(func=_cmd_observe)

    pr = sub.add_parser("promote")
    common(pr)
    pr.add_argument("--to", choices=["shadow", "canary", "active"], required=True)
    pr.set_defaults(func=_cmd_promote)

    rb = sub.add_parser("rollback")
    common(rb)
    rb.add_argument("--reason", required=True)
    rb.set_defaults(func=_cmd_rollback)

    st = sub.add_parser("status")
    common(st, version=False)
    st.set_defaults(func=_cmd_status)

    args = parser.parse_args(argv)
    try:
        EvaluatorAccess.from_env()
    except SplitAccessError as exc:
        print(str(exc), file=sys.stderr)
        return 3
    try:
        return int(args.func(args, _Ctx(args.policy_file, args.policy_set)))
    except (PromotionBlockedError, RegistryError, SplitAccessError, RuntimeError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
