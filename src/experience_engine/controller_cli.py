"""ee-ctl: the adaptive controller's learning plane and rollout control (Milestone 6).

  ee-ctl train      --tenant T --policy-id P --kind cost_aware|bandit|evidence [--last N]
                    [--paired-from RESULTS ...]                      (M6.1 paired validation prior)
  ee-ctl evaluate   offline|shadow|canary --tenant T --policy-id P --version V
  ee-ctl promote    --policy-id P --version V --to shadow|canary|active
  ee-ctl rollback | reject | status | replay
  ee-ctl simulate   --heldout NAME RESULTS ... [--prior RESULTS ...] --out report.md
                                                                  (offline, full-information)
  ee-ctl prior-curve --prior RESULTS ... --sizes 30 60 120 240     (validation size vs resolution)
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

from experience_engine import RUNTIME_VERSION, drift_commands
from experience_engine.adapters.artifacts.local_fs import LocalFsArtifactStore
from experience_engine.adapters.clock import SystemClock
from experience_engine.adapters.db.controller_store import SqlControllerPolicyStore, SqlDecisionLog
from experience_engine.adapters.db.drift_store import SqlDriftLog
from experience_engine.adapters.db.engine import InfraSettings, make_engine
from experience_engine.adapters.db.registry_store import SqlRegistryStore
from experience_engine.adapters.db.run_lifecycle import SqlRunLifecycle
from experience_engine.bench.dataset import BenchTask, DatasetLoader, EvaluatorAccess
from experience_engine.bench.environment import ENVIRONMENT_VERSION, SimulatedEnvironment
from experience_engine.bench.verifier import SimulationVerifier
from experience_engine.controller.datasets import (
    EnvReport,
    from_experiment,
    load_paired,
    load_prior,
    with_protected_share,
)
from experience_engine.controller.drift_service import DriftService
from experience_engine.controller.estimator import build_snapshot
from experience_engine.controller.evidence_quality import (
    ServingContext,
    assess_evidence,
    source_and_cell,
)
from experience_engine.controller.gates import (
    ShadowPair,
    evaluate_canary,
    evaluate_offline,
    evaluate_shadow,
)
from experience_engine.controller.paired import PairedPrior, resolution_curve, version_problems
from experience_engine.controller.registry import ControllerPolicyRegistry, ControllerRegistryError
from experience_engine.controller.replay import replay_decision
from experience_engine.controller.report import SimEntry, SimSet, render_sim_report
from experience_engine.controller.rollout import build_rollout
from experience_engine.controller.simulate import SimConfig, SimDrift, evaluate, rule_log
from experience_engine.controller_fixtures import portfolio
from experience_engine.domain.benchmark import AgentConfig, DatasetVersion, Split
from experience_engine.domain.controller import (
    PLANNING_KEY,
    ControllerKind,
    ControllerParams,
    ControllerPolicyDefinition,
    PairedCell,
    PairedSource,
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


KIND_NAMES = {
    "cost_aware": ControllerKind.COST_AWARE,
    "bandit": ControllerKind.BANDIT,
    "evidence": ControllerKind.EVIDENCE_DRIVEN,
}


def _params(
    args: argparse.Namespace, kind: str | None = None, with_evidence: bool = False
) -> ControllerParams:
    kind = kind or args.kind
    raw: dict[str, object] = {
        "n_min": args.n_min,
        "min_total_verified": args.min_total_verified,
        "min_cost_saving": args.min_cost_saving,
    }
    if kind in ("bandit", "evidence"):
        raw["exploration_families"] = list(args.explore_families or [])
        raw["exploration_risk_levels"] = ["low"] if args.explore_families else []
        raw["epsilon"] = args.epsilon
    drift_mode = getattr(args, "drift_monitor", None)
    if with_evidence or kind == "evidence" or drift_mode:
        evidence: dict[str, object] = {}
        drift_min_n = getattr(args, "drift_min_n", None)
        if drift_min_n is not None:
            evidence["drift_min_n"] = drift_min_n
        if getattr(args, "paired_from_registry", None) and not getattr(
            args, "allow_research", False
        ):
            evidence["require_production_eligible"] = True
        raw["evidence"] = evidence
    if drift_mode:
        monitor: dict[str, object] = {"mode": drift_mode.upper()}
        for src, dst in (
            ("drift_detector", "detector"),
            ("drift_confidence", "confidence"),
            ("drift_persistence", "persistence"),
        ):
            if getattr(args, src, None) is not None:
                monitor[dst] = getattr(args, src)
        raw["drift"] = monitor
    return ControllerParams.model_validate(raw)


def _registry_prior(args: argparse.Namespace, params: ControllerParams) -> PairedPrior:
    """A prior from stored promotion evaluations (final-outcome evidence), eligibility-checked.

    Ineligible evidence cannot seed a production prior (exit 2 with every failed gate listed);
    `--allow-research` takes it anyway and records it as not production-eligible."""
    assert params.evidence is not None
    engine = make_engine(InfraSettings.from_env().database_url)
    registry = SqlRegistryStore(engine)
    ctx = ServingContext(
        tenant_id=args.tenant,
        runtime_version=RUNTIME_VERSION,
        verifier_version=SimulationVerifier().version,
        environment_version=ENVIRONMENT_VERSION,
        n_min_pairs=params.evidence.n_min_pairs,
        max_age_days=params.drift.max_evidence_age_days if params.drift else None,
        now=SystemClock().now(),
        allow_version_mismatch=args.allow_version_mismatch,
    )
    cells: dict[str, PairedCell] = {}
    sources: list[PairedSource] = []
    seen: set[str] = set()
    failures: list[str] = []
    for ref in args.paired_from_registry:
        cap, _, ver = ref.partition("@")
        recs = [
            r
            for r in registry.evaluation_records(args.tenant, cap, int(ver))
            if r.serving is not None
        ]
        if args.evaluation_id:
            recs = [r for r in recs if r.evaluation_id in args.evaluation_id]
        if not recs:
            raise SystemExit(
                f"no stored evaluation of {ref} recorded final-outcome evidence "
                "(run `ee-eval evaluate --serving-evidence`)"
            )
        rec = recs[-1]  # the latest evaluation; earlier ones stay in the registry untouched
        key, cell, source, facts = source_and_cell(rec)
        a = assess_evidence(key, cell, facts, ctx, seen_evaluations=frozenset(seen))
        seen.add(rec.evaluation_id)
        print(
            f"evidence {ref} (evaluation {rec.evaluation_id[:8]}): "
            f"{'ELIGIBLE' if a.eligible else 'NOT eligible'}; {cell.n} verified pairs"
        )
        for why in a.reasons:
            print(f"  - {why}")
        for note in a.overrides:
            print(f"  ! override recorded: {note}")
        if not a.eligible:
            failures += [f"{ref}: {why}" for why in a.reasons]
        if a.eligible or args.allow_research:
            cells[key] = cell
            sources.append(
                source.model_copy(
                    update={"production_eligible": a.eligible, "overrides": a.overrides}
                )
            )
    if failures and not args.allow_research:
        raise SystemExit(
            "no production-eligible prior can be built (use --allow-research to record the "
            "evidence as research-only):\n  " + "\n  ".join(failures)
        )
    return PairedPrior(cells=cells, sources=sources)


def _prior_from(args: argparse.Namespace, params: ControllerParams) -> PairedPrior | None:
    """The paired prior of `--paired-from` (research files) or `--paired-from-registry`."""
    if getattr(args, "paired_from_registry", None):
        return _registry_prior(args, params)
    if not args.paired_from:
        return None
    assert params.evidence is not None
    prior = load_prior(
        args.paired_from, families=args.paired_families, n_min_pairs=params.evidence.n_min_pairs
    )
    verifier = SimulationVerifier().version
    problems = [p for s in prior.sources for p in version_problems(s, RUNTIME_VERSION, verifier)]
    if problems and not args.allow_version_mismatch:
        raise SystemExit(
            "paired sources do not match this runtime/verifier (use --allow-version-mismatch to "
            "override and record it):\n  " + "\n  ".join(problems)
        )
    if not prior.cells:
        raise SystemExit(
            f"no (family, alternative) cell reaches n_min_pairs={params.evidence.n_min_pairs}; "
            f"excluded: {prior.excluded}"
        )
    return prior


# ---- registry commands --------------------------------------------------------------------------
def _cmd_train(args: argparse.Namespace) -> int:
    store, log = _store(args)
    records = log.outcome_records(args.tenant, policy_versions=args.policy_versions)
    if args.last:
        records = records[-args.last :]
    params = _params(args, with_evidence=bool(args.paired_from or args.paired_from_registry))
    prior = _prior_from(args, params)
    snap = build_snapshot(records, args.tenant, params, prior=prior)
    version = max((r.version for r in store.list_versions(args.tenant, args.policy_id)), default=0)
    definition = ControllerPolicyDefinition(
        policy_id=args.policy_id,
        version=version + 1,
        kind=KIND_NAMES[args.kind],
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
    if prior is not None:
        print(
            f"paired prior: {len(prior.cells)} cell(s), {sum(c.n for c in prior.cells.values())} "
            f"pairs from {len(prior.sources)} source(s), {prior.validation_tokens:,} validation "
            f"tokens (learning cost); excluded below n_min_pairs: {prior.excluded or 'none'}"
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
    if args.stage == "canary":
        # a canary FAIL rolls the candidate back immediately (harm must not keep serving traffic)
        if reg.apply_canary_guard(args.tenant, args.policy_id, args.version, report):
            print(f"canary FAIL: {args.policy_id}/{args.version} rolled back automatically")
    else:
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
def _prior_note(prior: PairedPrior) -> str:
    cells = ", ".join(f"`{k}` n={c.n}" for k, c in prior.cells.items())
    return (
        f" Paired prior (for the `+prior` controllers): {cells or 'no cell'}; "
        f"{prior.validation_tokens:,} validation tokens; excluded below n_min_pairs: "
        f"{prior.excluded or 'none'}."
    )


SIM_KINDS = ("cost_aware", "bandit", "evidence", "cost_aware+prior", "evidence+prior")


def _cmd_simulate(args: argparse.Namespace) -> int:
    warm_tasks = []
    for path in args.warm or []:
        res = ExperimentResult.model_validate_json(Path(path).read_text())
        warm_tasks += from_experiment(res, families=args.families)[0]
    warm = rule_log(warm_tasks) if warm_tasks else []
    prior = (
        load_prior(
            args.prior,
            families=args.families,
            subsample_n=args.prior_subsample,
            subsample_families=args.prior_subsample_families,
        )
        if args.prior
        else None
    )
    if any(k.endswith("+prior") for k in args.kinds) and prior is None:
        raise SystemExit("a '+prior' kind needs --prior RESULTS ...")
    sets: list[SimSet] = []
    for name, *paths in args.heldout:
        tasks, env = [], EnvReport(0, 0, 0)
        for i, path in enumerate(paths):
            res = ExperimentResult.model_validate_json(Path(path).read_text())
            part, e = from_experiment(
                res, families=args.families, id_prefix=f"h{i}:" if len(paths) > 1 else ""
            )
            tasks += part
            env = EnvReport(
                env.n_pairs + e.n_pairs,
                env.n_unknown_dropped + e.n_unknown_dropped,
                env.n_without_capability + e.n_without_capability,
            )
        if args.protected_share:
            tasks = with_protected_share(tasks, args.protected_share)
        entries = []
        for label in args.kinds:
            base, with_prior = label.removesuffix("+prior"), label.endswith("+prior")
            cfg = SimConfig(
                KIND_NAMES[base],
                _params(args, kind=base, with_evidence=with_prior),
                refresh_every=args.refresh_every,
                prior=prior if with_prior else None,
                drift=SimDrift(
                    int(args.drift_start * len(tasks)), PLANNING_KEY, args.drift_survival
                )
                if args.drift_start
                else None,
            )
            cmp, _ = evaluate(
                tasks,
                cfg,
                n_orders=args.orders,
                seed=args.seed,
                warm=warm,
                horizon=args.horizon or None,
            )
            entries.append(SimEntry(label, cfg, cmp))
        sets.append(
            SimSet(
                name,
                len(tasks),
                f"{env.n_pairs} (task, repetition) pairs; {env.n_unknown_dropped} UNKNOWN dropped; "
                f"{env.n_without_capability} without an eligible capability."
                + (_prior_note(prior) if prior is not None else ""),
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
                            "families": {
                                fam: {
                                    "n_tasks": fr.n_tasks,
                                    "success_diff": fr.success_diff,
                                    "cost_ratio": fr.cost_ratio,
                                    "verdict_cell": fr.verdict_cell.value,
                                    "verdict_a2": fr.verdict_a2.value,
                                    "deviation_rate": fr.deviation_rate,
                                    "exploration_rate": fr.exploration_rate,
                                    "exploration_successes_lost_per_order": fr.exploration_successes_lost_per_order,
                                    "exploration_tokens_delta_per_order": fr.exploration_tokens_delta_per_order,
                                }
                                for fam, fr in e.comparison.family_results.items()
                            },
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


def _cmd_prior_curve(args: argparse.Namespace) -> int:
    params = ControllerParams()
    tasks, sources = load_paired(args.prior, families=args.families)
    rows = resolution_curve(
        tasks,
        args.sizes,
        draws=args.draws,
        delta=params.delta[args.risk],
        confidence=params.confidence,
        seed=args.seed,
    )
    lines = [
        "# Prior validation size: how often does a paired prior resolve? (generated)",
        "",
        f"Sources: {', '.join(s.name for s in sources)}; {len(tasks)} paired tasks; margin "
        f"delta={params.delta[args.risk]}, one-sided confidence {params.confidence}; {args.draws} "
        f"bootstrap draws of n tasks (with replacement) per row, seed {args.seed}. Descriptive: the "
        "recorded pairs stand in for the population; no held-out or live data is used.",
        "",
        "| cell | n paired tasks | P(proven non-inferior) | P(proven worse) | P(unresolved) |",
        "|---|---|---|---|---|",
    ]
    lines += [
        f"| `{r.key}` | {r.n} | {r.p_admissible:.2f} | {r.p_worse:.2f} | {r.p_unresolved:.2f} |"
        for r in rows
    ]
    text = "\n".join(lines) + "\n"
    if args.out:
        Path(args.out).write_text(text)
    print(text)
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
    drift = DriftService(
        lambda tenant, versions: SqlDecisionLog(engine).outcome_records(
            tenant, policy_versions=versions
        ),
        SqlDriftLog(engine),
        SystemClock().now,
    )
    setup = build_rollout(
        store,
        args.tenant,
        args.policy_id,
        RuleCapabilityPolicy(),
        args.canary_fraction,
        drift=drift,
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
    p.add_argument("--kind", choices=sorted(KIND_NAMES), default="cost_aware")
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
    tr.add_argument(
        "--paired-from",
        nargs="+",
        default=None,
        metavar="RESULTS",
        help="held-out arm A/E experiment files seeding a paired prior (M6.1)",
    )
    tr.add_argument("--paired-families", nargs="*", default=None)
    tr.add_argument(
        "--paired-from-registry",
        nargs="+",
        default=None,
        metavar="CAPABILITY@VERSION",
        help="build the prior from stored promotion evaluations (M6.2): eligibility-checked "
        "final-outcome evidence; implies the production-eligibility gate",
    )
    tr.add_argument("--evaluation-id", nargs="*", default=None)
    tr.add_argument(
        "--allow-research",
        action="store_true",
        help="with --paired-from-registry: accept ineligible evidence, recorded as research-only",
    )
    tr.add_argument(
        "--drift-monitor",
        choices=["shadow", "enforce"],
        default=None,
        help="attach the M6.2 windowed drift monitor (needs a paired prior)",
    )
    tr.add_argument("--drift-detector", choices=["WINDOW", "CUSUM"], default=None)
    tr.add_argument("--drift-confidence", type=float, default=None)
    tr.add_argument("--drift-persistence", type=int, default=None)
    tr.add_argument(
        "--allow-version-mismatch",
        action="store_true",
        help="accept paired sources recorded under another runtime or verifier version",
    )
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
        "--heldout",
        nargs="+",
        action="append",
        metavar=("NAME", "RESULTS"),
        required=True,
        help="NAME RESULTS [RESULTS ...]: several result files are merged into one stream",
    )
    sm.add_argument(
        "--prior",
        nargs="+",
        default=None,
        metavar="RESULTS",
        help="held-out arm A/E files for the paired prior of the '+prior' kinds",
    )
    sm.add_argument("--prior-subsample", type=int, default=0, help="keep N tasks per family")
    sm.add_argument(
        "--drift-start",
        type=float,
        default=0.0,
        help="SYNTHETIC: fraction of the stream after which planning's observed successes degrade",
    )
    sm.add_argument("--drift-survival", type=float, default=0.2)
    sm.add_argument(
        "--drift-min-n",
        type=int,
        default=None,
        help="live outcomes before the evidence drift guard acts (default 30; huge = guard off)",
    )
    sm.add_argument("--prior-subsample-families", nargs="*", default=None)
    sm.add_argument("--warm", nargs="*", default=None, help="recorded runs used as a rule-only log")
    sm.add_argument("--kinds", nargs="+", choices=SIM_KINDS, default=["cost_aware", "bandit"])
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

    pc = sub.add_parser(
        "prior-curve", help="how often a paired prior of n validation tasks resolves a cell"
    )
    pc.add_argument("--prior", nargs="+", required=True, metavar="RESULTS")
    pc.add_argument("--families", nargs="*", default=None)
    pc.add_argument("--sizes", nargs="+", type=int, default=[30, 60, 120, 240, 360])
    pc.add_argument("--draws", type=int, default=1000)
    pc.add_argument("--risk", choices=["low", "medium"], default="low")
    pc.add_argument("--seed", type=int, default=20260602)
    pc.add_argument("--out", default=None)
    pc.set_defaults(func=_cmd_prior_curve)

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

    drift_commands.register(sub)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
