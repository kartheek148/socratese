"""ee-ctl: the adaptive controller's learning plane and rollout control (Milestone 6).

  ee-ctl train      --tenant T --policy-id P --kind cost_aware|bandit [--last N] ...
  ee-ctl evaluate   offline|shadow|canary --tenant T --policy-id P --version V
  ee-ctl promote    --policy-id P --version V --to shadow|canary|active
  ee-ctl rollback | reject | status | replay
  ee-ctl simulate   --heldout NAME RESULTS ... --out report.md      (offline, full-information)
  ee-ctl paired-run --portfolio good|heterogeneous|drifted ...      (real model, arms A and E)
  ee-ctl live       --policy-id P --portfolio ...                   (registry-driven shadow/canary)

Training and evaluation read the decision log; nothing here is on the execution path. A policy
only reaches production through `promote`, which needs stored PASS evidence for the previous
stage (and the database re-checks it).
"""

# ruff: noqa: E501  (status/report print lines)

import argparse
import json
import os
import sys
from collections import defaultdict
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from experience_engine import RUNTIME_VERSION
from experience_engine.adapters.artifacts.local_fs import LocalFsArtifactStore
from experience_engine.adapters.clock import SystemClock
from experience_engine.adapters.db.controller_store import SqlControllerPolicyStore, SqlDecisionLog
from experience_engine.adapters.db.engine import InfraSettings, make_engine
from experience_engine.adapters.db.run_lifecycle import SqlRunLifecycle
from experience_engine.bench.dataset import BenchTask, DatasetLoader, EvaluatorAccess
from experience_engine.bench.environment import ENVIRONMENT_VERSION, SimulatedEnvironment
from experience_engine.bench.verifier import SimulationVerifier
from experience_engine.controller.datasets import from_experiment, with_protected_share
from experience_engine.controller.estimator import build_snapshot
from experience_engine.controller.gates import (
    ShadowPair,
    evaluate_canary,
    evaluate_offline,
    evaluate_shadow,
)
from experience_engine.controller.registry import ControllerPolicyRegistry, ControllerRegistryError
from experience_engine.controller.replay import replay_decision
from experience_engine.controller.report import SimEntry, SimSet, render_sim_report
from experience_engine.controller.rollout import build_rollout
from experience_engine.controller.simulate import SimConfig, evaluate, rule_log
from experience_engine.controller_fixtures import portfolio
from experience_engine.domain.benchmark import AgentConfig, DatasetVersion, Split
from experience_engine.domain.controller import (
    ControllerKind,
    ControllerParams,
    ControllerPolicyDefinition,
)
from experience_engine.domain.experiment import (
    ExperimentConfig,
    ExperimentResult,
    LearningLedger,
)
from experience_engine.domain.rollout import PolicyState, Stage
from experience_engine.eval_cli import _build_llm
from experience_engine.evaluation.arms import ArmRunner
from experience_engine.evaluation.experiment import ExperimentTask, run_experiment, tag_tasks
from experience_engine.evaluation.experiment_arms import make_baseline_arm, make_runtime_arm
from experience_engine.execution.capability_runtime import (
    CapabilityRuntime,
    StaticCapabilityCatalog,
)
from experience_engine.execution.controller import RuleCapabilityPolicy
from experience_engine.execution.matcher import CapabilityMatcher
from experience_engine.execution.orchestrator import TaskOrchestrator
from experience_engine.execution.protocol import PROMPT_VERSION

ACTOR = "ee-ctl"
RULE_VERSION = RuleCapabilityPolicy.policy_version


def _store(args: argparse.Namespace) -> tuple[SqlControllerPolicyStore, SqlDecisionLog]:
    engine = make_engine(InfraSettings.from_env().database_url)
    return SqlControllerPolicyStore(engine), SqlDecisionLog(engine)


def _registry(store: SqlControllerPolicyStore) -> ControllerPolicyRegistry:
    return ControllerPolicyRegistry(store, SystemClock())


def _params(args: argparse.Namespace) -> ControllerParams:
    raw: dict[str, object] = {
        "n_min": args.n_min,
        "min_total_verified": args.min_total_verified,
        "min_cost_saving": args.min_cost_saving,
    }
    if args.kind == "bandit":
        raw["exploration_families"] = list(args.explore_families or [])
        raw["exploration_risk_levels"] = ["low"] if args.explore_families else []
        raw["epsilon"] = args.epsilon
    return ControllerParams.model_validate(raw)


# ---- registry commands --------------------------------------------------------------------------
def _cmd_train(args: argparse.Namespace) -> int:
    store, log = _store(args)
    records = log.outcome_records(args.tenant, policy_versions=args.policy_versions)
    if args.last:
        records = records[-args.last :]
    params = _params(args)
    snap = build_snapshot(records, args.tenant, params)
    version = max((r.version for r in store.list_versions(args.tenant, args.policy_id)), default=0)
    definition = ControllerPolicyDefinition(
        policy_id=args.policy_id,
        version=version + 1,
        kind=ControllerKind(args.kind.upper()),
        params=params,
        snapshot=snap,
    )
    _registry(store).register(definition)
    cal = snap.calibration
    print(
        f"registered {definition.policy_version} ({definition.kind.value}) CANDIDATE "
        f"hash={definition.definition_hash()[:12]}; trained on {snap.n_verified} verified "
        f"decisions ({snap.provenance.n_unknown_excluded if snap.provenance else 0} UNKNOWN and "
        f"{snap.provenance.n_unacted_excluded if snap.provenance else 0} unacted excluded); "
        f"calibration n={cal.n if cal else 0} ece={cal.ece if cal else None} ok={cal.ok if cal else False}"
    )
    return 0


def _print_report(rep: object) -> None:
    print(json.dumps(json.loads(rep.model_dump_json()), indent=1))  # type: ignore[attr-defined]


def _cmd_evaluate(args: argparse.Namespace) -> int:
    store, log = _store(args)
    reg = _registry(store)
    rec = store.get_version(args.tenant, args.policy_id, args.version)
    if rec is None:
        print("policy version not found for this tenant", file=sys.stderr)
        return 2
    d = rec.definition
    if args.stage == "offline":
        report = evaluate_offline(d, log.outcome_records(args.tenant), reg.gates)
    elif args.stage == "shadow":
        pairs = [ShadowPair(a, s) for a, s in log.shadow_pairs(args.tenant, d.policy_version)]
        errors = log.count_trace_events(args.tenant, "controller_shadow", "error")
        report = evaluate_shadow(d, pairs, errors, reg.gates)
    else:
        if rec.state != PolicyState.CANARY:
            print(
                f"canary evidence is only valid while the version is in CANARY (it is {rec.state.value})",
                file=sys.stderr,
            )
            return 2
        active = next(
            (
                r
                for r in store.list_versions(args.tenant, args.policy_id)
                if r.state == PolicyState.ACTIVE
            ),
            None,
        )
        control = active.definition.policy_version if active else RULE_VERSION
        started = max(
            (
                h.created_at
                for h in store.history(args.tenant, args.policy_id)
                if h.version == args.version and h.to_state == PolicyState.CANARY
            ),
            default=None,
        )
        if started is None:
            print("the candidate has not been in CANARY: no concurrent window", file=sys.stderr)
            return 2
        # concurrent control only: decisions made while this version was in CANARY
        rows = log.outcome_records(
            args.tenant, policy_versions=[d.policy_version, control], since=started
        )
        report = evaluate_canary(
            d,
            control,
            rows,
            reg.gates,
            control_adaptive_version=active.definition.policy_version if active else None,
        )
    reg.record_evaluation(args.tenant, report)
    _print_report(report)
    return 0 if report.verdict.value != "FAIL" else 1


def _cmd_promote(args: argparse.Namespace) -> int:
    store, _ = _store(args)
    try:
        _registry(store).promote(
            args.tenant,
            args.policy_id,
            args.version,
            PolicyState(args.to.upper()),
            ACTOR,
            args.reason,
        )
    except ControllerRegistryError as exc:
        print(f"blocked: {exc}", file=sys.stderr)
        return 1
    print(f"{args.policy_id}/{args.version} -> {args.to.upper()}")
    return 0


def _cmd_rollback(args: argparse.Namespace) -> int:
    store, _ = _store(args)
    try:
        _registry(store).rollback(args.tenant, args.policy_id, args.version, ACTOR, args.reason)
    except ControllerRegistryError as exc:
        print(f"blocked: {exc}", file=sys.stderr)
        return 1
    print(f"{args.policy_id}/{args.version} rolled back")
    return 0


def _cmd_reject(args: argparse.Namespace) -> int:
    store, _ = _store(args)
    try:
        _registry(store).reject(args.tenant, args.policy_id, args.version, ACTOR, args.reason)
    except ControllerRegistryError as exc:
        print(f"blocked: {exc}", file=sys.stderr)
        return 1
    print(f"{args.policy_id}/{args.version} rejected")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    store, _ = _store(args)
    for r in store.list_versions(args.tenant, args.policy_id):
        d = r.definition
        print(
            f"{d.policy_version:14} {r.state.value:11} {d.kind.value:10} hash={r.definition_hash[:12]} "
            f"verified={d.snapshot.n_verified} calibrated={d.snapshot.calibration_ok}"
        )
        for stage in Stage:
            ev = store.latest_evaluation(args.tenant, args.policy_id, r.version, stage)
            if ev is not None:
                print(
                    f"    {stage.value:8} {ev.report.verdict.value:12} {'; '.join(ev.report.reasons)[:100]}"
                )
    for h in store.history(args.tenant, args.policy_id):
        print(
            f"  {h.created_at:%H:%M:%S} v{h.version} {h.from_state.value}->{h.to_state.value} by {h.actor}: {h.reason[:60]}"
        )
    return 0


def _cmd_replay(args: argparse.Namespace) -> int:
    store, log = _store(args)
    rec = store.get_version(args.tenant, args.policy_id, args.version)
    if rec is None:
        print("policy version not found for this tenant", file=sys.stderr)
        return 2
    decisions = log.decisions(args.tenant, rec.definition.policy_version, limit=args.limit)
    bad = []
    for d in decisions:
        check = replay_decision(d, rec.definition)
        if not check.reproducible:
            bad.append((d.decision_id, check.differences))
    print(f"{len(decisions) - len(bad)}/{len(decisions)} stored decisions reproduced exactly")
    for did, diffs in bad[:10]:
        print(f"  NOT reproducible {did}: {diffs}")
    return 0 if not bad else 1


# ---- offline simulation -------------------------------------------------------------------------
def _cmd_simulate(args: argparse.Namespace) -> int:
    warm_tasks = []
    for path in args.warm or []:
        res = ExperimentResult.model_validate_json(Path(path).read_text())
        warm_tasks += from_experiment(res, families=args.families)[0]
    warm = rule_log(warm_tasks) if warm_tasks else []
    sets: list[SimSet] = []
    for name, path in args.heldout:
        res = ExperimentResult.model_validate_json(Path(path).read_text())
        tasks, env = from_experiment(res, families=args.families)
        if args.protected_share:
            tasks = with_protected_share(tasks, args.protected_share)
        entries = []
        for kind in args.kinds:
            ns = argparse.Namespace(**{**vars(args), "kind": kind})
            cfg = SimConfig(
                ControllerKind(kind.upper()), _params(ns), refresh_every=args.refresh_every
            )
            cmp, _ = evaluate(
                tasks,
                cfg,
                n_orders=args.orders,
                seed=args.seed,
                warm=warm,
                horizon=args.horizon or None,
            )
            entries.append(SimEntry(kind, cfg, cmp))
        sets.append(
            SimSet(
                name,
                len(tasks),
                f"{env.n_pairs} (task, repetition) pairs; {env.n_unknown_dropped} UNKNOWN dropped; "
                f"{env.n_without_capability} without an eligible capability.",
                tasks,
                entries,
            )
        )
    text = render_sim_report(sets, args.title, args.preamble or "")
    Path(args.out).write_text(text)
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(
                {
                    s.name: {
                        e.kind: {
                            "verdict": e.comparison.verdict.value,
                            "success_diff": e.comparison.success_diff,
                            "cost_ratio": e.comparison.cost_ratio,
                            "deviation_rate": e.comparison.deviation_rate,
                            "exploration_rate": e.comparison.exploration_rate,
                        }
                        for e in s.entries
                    }
                    for s in sets
                },
                indent=1,
            )
        )
    print(f"wrote {args.out}")
    return 0


# ---- real-model drivers -------------------------------------------------------------------------
def _tasks(args: argparse.Namespace, splits: Sequence[str]) -> list[ExperimentTask]:
    private = args.private_root or os.environ.get("EE_PRIVATE_DATASET_ROOT")
    if not private:
        raise SystemExit("set --private-root or EE_PRIVATE_DATASET_ROOT")
    loader = DatasetLoader(Path(args.root), EvaluatorAccess.from_env(), private_root=Path(private))
    out: list[ExperimentTask] = []
    for name in splits:
        out += tag_tasks(name, loader.load(Split[name]).tasks, {"pool", "timeout"})
    if args.families:
        out = [t for t in out if t.bench.family in args.families]
    out = out[args.offset :]
    return out[: args.max_tasks] if args.max_tasks else out


def _version(v: DatasetVersion) -> dict[str, str]:
    return {"name": v.name, "version": v.version, "content_hash": v.content_hash}


def _cmd_paired_run(args: argparse.Namespace) -> int:
    llm, decisions, model = _build_llm(args.llm, 0)
    verifier, clock = SimulationVerifier(), SystemClock()
    defs = portfolio(args.portfolio)
    catalog = StaticCapabilityCatalog(args.tenant, defs)
    runtime = CapabilityRuntime(
        catalog=catalog,
        matcher=CapabilityMatcher(catalog),
        decisions_for=lambda _llm: decisions,
        experimental=True,
    )

    def cfg(config_id: str) -> AgentConfig:
        return AgentConfig(config_id=config_id, prompt_version=PROMPT_VERSION, model=model)

    arms: dict[str, ArmRunner] = {
        "A": make_baseline_arm(llm=llm, config=cfg("A_BASELINE"), verifier=verifier, clock=clock),
        "E": make_runtime_arm(
            llm=llm,
            config=cfg("E_STRESS"),
            verifier=verifier,
            clock=clock,
            runtime=runtime,
            tenant_id=args.tenant,
        ),
    }
    tasks = _tasks(args, ["VALIDATION", "HIDDEN_TRANSFER"])
    rows = run_experiment(arms, tasks, repetitions=args.reps, workers=args.workers)
    loader = DatasetLoader(
        Path(args.root),
        EvaluatorAccess.from_env(),
        private_root=Path(args.private_root or os.environ["EE_PRIVATE_DATASET_ROOT"]),
    )
    config = ExperimentConfig(
        name=args.name,
        model=model.model_dump(mode="json", exclude={"seed"}),
        arms=list(arms),
        repetitions=args.reps,
        workers=args.workers,
        datasets={
            "VALIDATION": _version(loader.load(Split.VALIDATION).version),
            "HIDDEN_TRANSFER": _version(loader.load(Split.HIDDEN_TRANSFER).version),
        },
        discovery_trajectories=0,
        discovery_families=["pool", "timeout"],
        capabilities=[f"{d.id}@{d.version}" for d in defs],
        capability_catalog_states=["STRESS_FIXTURE"],
        memory_preamble_sha256="",
        max_episodes=0,
        verifier_version=verifier.version,
        runtime_version=RUNTIME_VERSION,
        evaluator_version="m6-paired/1",
        mode="stress",
        notes=[
            f"environment {ENVIRONMENT_VERSION}",
            f"stress-fixture portfolio {args.portfolio!r} (hand-authored, never promoted)",
        ],
    )
    result = ExperimentResult(
        config=config, ledger=LearningLedger(discovery_tokens=0), rows=rows, applicability=[]
    )
    Path(args.out).write_text(result.model_dump_json(indent=1))
    n_unknown = sum(1 for r in rows if r.status == "UNKNOWN")
    print(f"wrote {args.out}: {len(rows)} runs ({n_unknown} UNKNOWN) over {len(tasks)} tasks")
    return 0


def _cmd_live(args: argparse.Namespace) -> int:
    infra = InfraSettings.from_env()
    engine = make_engine(infra.database_url)
    store = SqlControllerPolicyStore(engine)
    setup = build_rollout(
        store, args.tenant, args.policy_id, RuleCapabilityPolicy(), args.canary_fraction
    )
    llm, decisions, model = _build_llm(args.llm, 0)
    defs = portfolio(args.portfolio)
    catalog = StaticCapabilityCatalog(args.tenant, defs)
    runtime = CapabilityRuntime(
        catalog=catalog,
        matcher=CapabilityMatcher(catalog),
        decisions_for=lambda _llm: decisions,
        experimental=True,
    )
    orchestrator = TaskOrchestrator(
        controller=setup.controller,
        verifier=SimulationVerifier(),
        clock=SystemClock(),
        config=AgentConfig(config_id="M6_LIVE", prompt_version=PROMPT_VERSION, model=model),
        lifecycle=SqlRunLifecycle(engine),
        artifacts=LocalFsArtifactStore(infra.artifact_root),
        capabilities=runtime,
        shadow=setup.shadow,
    )
    tasks = _tasks(args, ["VALIDATION", "HIDDEN_TRANSFER"])
    print(
        f"live: active={setup.active_version} canary={setup.canary_version} "
        f"shadow={setup.shadow_version} tasks={len(tasks)}"
    )

    def one(et: ExperimentTask) -> tuple[str, str, int]:
        b: BenchTask = et.bench
        try:
            r = orchestrator.execute(
                b.task,
                SimulatedEnvironment(b.environment, b.task.allowed_tools),
                llm,
                run_tags={"stress_fixture": args.portfolio, "variant": b.variant},
            )
            return r.decision.policy_version, r.outcome.status.value, r.outcome.tokens_used
        except Exception as exc:
            return "ERROR", type(exc).__name__, 0

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(one, tasks))
    by: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for pv, st, tok in results:
        by[pv].append((st, tok))
    for pv, v in sorted(by.items()):
        ok = sum(s == "VERIFIED_SUCCESS" for s, _ in v)
        print(f"  {pv}: n={len(v)} verified={ok} tokens={sum(t for _, t in v)}")
    return 0


# ---- parser -------------------------------------------------------------------------------------
def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--tenant", default="bench")
    p.add_argument("--policy-id", default="adaptive")


def _tuning(p: argparse.ArgumentParser) -> None:
    p.add_argument("--kind", choices=["cost_aware", "bandit"], default="cost_aware")
    p.add_argument("--explore-families", nargs="*", default=None)
    p.add_argument("--epsilon", type=float, default=0.10)
    p.add_argument("--n-min", type=int, default=30)
    p.add_argument("--min-total-verified", type=int, default=100)
    p.add_argument("--min-cost-saving", type=float, default=0.10)


def _stream(p: argparse.ArgumentParser) -> None:
    p.add_argument("--root", required=True, help="public dataset root")
    p.add_argument("--private-root", default=None)
    p.add_argument("--portfolio", choices=["good", "heterogeneous", "drifted"], required=True)
    p.add_argument("--llm", choices=["fake", "vertex"], default="vertex")
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--families", nargs="*", default=None)
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--max-tasks", type=int, default=0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ee-ctl")
    sub = parser.add_subparsers(dest="command", required=True)

    tr = sub.add_parser("train", help="train a controller policy from the decision log (CANDIDATE)")
    _common(tr)
    _tuning(tr)
    tr.add_argument("--last", type=int, default=0, help="train on the last N logged decisions")
    tr.add_argument("--policy-versions", nargs="*", default=None)
    tr.set_defaults(func=_cmd_train)

    ev = sub.add_parser("evaluate", help="record offline / shadow / canary evidence")
    ev.add_argument("stage", choices=["offline", "shadow", "canary"])
    _common(ev)
    ev.add_argument("--version", type=int, required=True)
    ev.set_defaults(func=_cmd_evaluate)

    for name, fn, extra in (
        ("promote", _cmd_promote, True),
        ("rollback", _cmd_rollback, False),
        ("reject", _cmd_reject, False),
    ):
        sp = sub.add_parser(name)
        _common(sp)
        sp.add_argument("--version", type=int, required=True)
        sp.add_argument("--reason", default=f"{name} via ee-ctl")
        if extra:
            sp.add_argument("--to", choices=["shadow", "canary", "active"], required=True)
        sp.set_defaults(func=fn)

    st = sub.add_parser("status", help="versions, evidence and history of a policy")
    _common(st)
    st.set_defaults(func=_cmd_status)

    rp = sub.add_parser("replay", help="verify stored decisions reproduce from features + version")
    _common(rp)
    rp.add_argument("--version", type=int, required=True)
    rp.add_argument("--limit", type=int, default=None)
    rp.set_defaults(func=_cmd_replay)

    sm = sub.add_parser("simulate", help="prequential full-information evaluation (offline)")
    sm.add_argument(
        "--heldout", nargs=2, action="append", metavar=("NAME", "RESULTS"), required=True
    )
    sm.add_argument("--warm", nargs="*", default=None, help="recorded runs used as a rule-only log")
    sm.add_argument(
        "--kinds", nargs="+", choices=["cost_aware", "bandit"], default=["cost_aware", "bandit"]
    )
    sm.add_argument("--explore-families", nargs="*", default=None)
    sm.add_argument("--epsilon", type=float, default=0.10)
    sm.add_argument("--n-min", type=int, default=30)
    sm.add_argument("--min-total-verified", type=int, default=100)
    sm.add_argument("--min-cost-saving", type=float, default=0.10)
    sm.add_argument("--families", nargs="*", default=None)
    sm.add_argument("--orders", type=int, default=20)
    sm.add_argument("--seed", type=int, default=20260601)
    sm.add_argument("--refresh-every", type=int, default=10)
    sm.add_argument(
        "--horizon",
        type=int,
        default=0,
        help="exploratory: streams of N tasks resampled with replacement from the recorded table",
    )
    sm.add_argument("--protected-share", type=float, default=0.0)
    sm.add_argument("--title", default="Adaptive controller: prequential replay (generated)")
    sm.add_argument("--preamble", default="")
    sm.add_argument("--out", required=True)
    sm.add_argument("--json-out", default=None)
    sm.set_defaults(func=_cmd_simulate)

    pr = sub.add_parser("paired-run", help="real-model arms A and E over a stress portfolio")
    _stream(pr)
    pr.add_argument("--tenant", default="bench")
    pr.add_argument("--reps", type=int, default=1)
    pr.add_argument("--name", default="m6 paired run")
    pr.add_argument("--out", required=True)
    pr.set_defaults(func=_cmd_paired_run)

    lv = sub.add_parser("live", help="persisted runs under the registry's rollout (shadow/canary)")
    _stream(lv)
    _common(lv)
    lv.add_argument("--canary-fraction", type=float, default=0.2)
    lv.set_defaults(func=_cmd_live)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
