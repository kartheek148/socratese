"""ee-ctl revalidation commands (M6.3): the frozen experiment, its report, and the database-backed
status / evaluate / replay of persisted revalidation evidence."""

import argparse
import dataclasses
import json
import multiprocessing
from pathlib import Path
from typing import Any

from experience_engine.controller.reval_eval import (
    REVAL_VARIANTS,
    RevalResult,
    result_from_json,
    result_to_json,
    run_reval_stream,
)
from experience_engine.controller.scenarios import REVAL_SCENARIOS
from experience_engine.domain.drift import DriftParams
from experience_engine.domain.revalidation import RevalidationParams, RevalStrategy


def _overrides(items: list[str] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for item in items or []:
        key, _, raw = item.partition("=")
        out[key] = json.loads(raw)
    return out


def _sim_worker(job: tuple[str, int, str, str, list[str], int | None]) -> list[dict[str, Any]]:
    sid, seed, drift_json, reval_json, variants, horizon = job
    scenario = REVAL_SCENARIOS[sid]
    if horizon is not None:  # EXPLORATORY: a longer stream after the same events
        scenario = dataclasses.replace(scenario, n=horizon)
    results = run_reval_stream(
        scenario,
        seed,
        DriftParams.model_validate_json(drift_json),
        RevalidationParams.model_validate_json(reval_json),
        variants,
    )
    return [result_to_json(r) for r in results]


def _cmd_sim(args: argparse.Namespace) -> int:
    seeds = list(range(args.seed_start, args.seed_start + args.seeds))
    drift = DriftParams.model_validate(
        {**DriftParams().model_dump(mode="json"), **_overrides(args.drift_set)}
    )
    reval = RevalidationParams.model_validate(
        {
            **RevalidationParams(strategy=RevalStrategy.FIXED).model_dump(mode="json"),
            **_overrides(args.set),
        }
    )
    variants = list(args.variants)
    jobs = [
        (sid, s, drift.model_dump_json(), reval.model_dump_json(), variants, args.horizon)
        for sid in args.scenarios
        for s in seeds
    ]
    raw: list[dict[str, Any]] = []
    ctx = multiprocessing.get_context("fork")
    with ctx.Pool(max(1, args.workers)) as pool:
        for batch in pool.imap(_sim_worker, jobs, chunksize=1):
            raw.extend(batch)
    raw.sort(key=lambda d: (d["sid"], d["variant"], d["seed"]))
    Path(args.json_out).write_text(json.dumps(raw))
    print(f"wrote {args.json_out} ({len(raw)} stream results; {len(jobs)} streams)")
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    from experience_engine.revalidation_report import report

    text = report(args.results, args.repro, args.title)
    Path(args.out).write_text(text)
    print(f"wrote {args.out}")
    return 0


# ---- exploratory real-model revalidation (no database) -------------------------------------------
def _cmd_real(args: argparse.Namespace) -> int:
    """One revalidation case on a recorded real prior: the real model, the production orchestrator
    and the independent verifier, fresh private tasks. Exploratory: it demonstrates the real
    execution path and measures real trial cost; it is not an acceptance experiment."""
    import time
    from datetime import UTC, datetime, timedelta

    from experience_engine import RUNTIME_VERSION, controller_cli
    from experience_engine.adapters.clock import SystemClock
    from experience_engine.bench.environment import SimulatedEnvironment
    from experience_engine.bench.verifier import SimulationVerifier
    from experience_engine.controller.datasets import load_prior
    from experience_engine.controller.drift import references_from_snapshot
    from experience_engine.controller.estimator import build_snapshot
    from experience_engine.controller.revalidation import CaseState, step_case
    from experience_engine.controller.revalidation_runtime import (
        OrchestratorTrialSource,
        RevalidationTask,
    )
    from experience_engine.domain.benchmark import AgentConfig
    from experience_engine.domain.controller import ControllerParams
    from experience_engine.domain.revalidation import (
        QuarantineReason,
        RevalState,
        TrialVerdict,
    )
    from experience_engine.eval_cli import _build_llm
    from experience_engine.execution.orchestrator import TaskOrchestrator
    from experience_engine.execution.protocol import PROMPT_VERSION

    prior = load_prior(args.prior, families=args.families)
    snap = build_snapshot([], args.tenant, ControllerParams.model_validate({"evidence": {}}),
                          prior=prior)  # fmt: skip
    ref = next(r for r in references_from_snapshot(snap) if r.family in args.families)
    llm, _decisions, model = _build_llm(args.llm, 0)
    verifier, clock = SimulationVerifier(), SystemClock()
    cfg = AgentConfig(config_id="REVALIDATION", prompt_version=PROMPT_VERSION,
                      model=model)  # fmt: skip
    tasks = [
        RevalidationTask(
            t.bench.task,
            lambda t=t: SimulatedEnvironment(t.bench.environment, t.bench.task.allowed_tools),  # type: ignore[misc]
            lambda: llm,
        )
        for t in controller_cli._tasks(args, ["VALIDATION", "HIDDEN_TRANSFER"])
    ]
    source = OrchestratorTrialSource(
        tenant_id=args.tenant,
        tasks=tasks,
        build=lambda c: TaskOrchestrator(controller=c, verifier=verifier, clock=clock, config=cfg),
        runtime_version=RUNTIME_VERSION,
        verifier_version=verifier.version,
        workers=args.workers,
        clock=lambda: datetime.now(UTC),
    )
    params = RevalidationParams(
        strategy=RevalStrategy(args.strategy),
        dwell=0,
        max_trials=args.max_trials,
        trials_per_round=args.round_size,
        round_hours=1.0,
        confirm_trials=args.confirm_trials,
        confirm_gap_hours=0.0,
        min_pool=1,
    )
    now = datetime.now(UTC)
    case = CaseState(
        cell_key=ref.cell_key, episode=1, reason=QuarantineReason(args.reason), severity=0.0,
        trigger_decision_id="real-0", trigger_at=now - timedelta(days=1),
        opened_at=now - timedelta(days=1), dwell=10_000,
    )  # fmt: skip
    transitions: list[Any] = []
    trials: list[Any] = []
    spent, t0 = 0, time.monotonic()
    print(f"reference: p_alt={ref.p_alt:.3f} mean_tokens={ref.mean_tokens:.0f} n={ref.n}; "
          f"{len(tasks)} fresh tasks; strategy {params.strategy.value}; "
          f"model {model.model}")  # fmt: skip
    for k in range(args.max_rounds):
        at = datetime.now(UTC)
        res = step_case(ref, params, case, execute=True, tenant_id=args.tenant, source=source,
                        facts=source.facts(ref, at), lifetime_spent=spent, anchor=f"real-{k}",
                        now=at)  # fmt: skip
        case, spent = res.case, spent + res.tokens
        transitions += res.transitions
        trials += res.trials
        for t in res.transitions:
            print(f"  round {k}: {t.from_state.value if t.from_state else '-'} -> "
                  f"{t.to_state.value} {t.trigger.value} n={t.n_trials} "
                  f"{'; '.join(t.detail)}")  # fmt: skip
        ok = sum(1 for x in trials if x.result.verdict == TrialVerdict.SUCCESS)
        print(f"  round {k}: {len(trials)} trials, {ok} successes, {spent:,} tokens, "
              f"{time.monotonic() - t0:.0f}s")  # fmt: skip
        if case.state in (RevalState.RECOVERED, RevalState.EXPIRED, RevalState.BUDGET_EXHAUSTED):
            break
    ev = [x for x in trials if x.key.stage.value == "EVIDENCE"]
    counted = [x for x in ev if x.result.counted]
    out = {
        "reference": {"p_alt": ref.p_alt, "mean_tokens": ref.mean_tokens, "n": ref.n},
        "params": params.model_dump(mode="json"),
        "final_state": case.state.value,
        "trials": len(trials),
        "evidence_successes": sum(1 for x in counted if x.result.success),
        "evidence_counted": len(counted),
        "tokens": spent,
        "unconsumed_prefetched_tokens": source.close(),
        "mean_tokens_per_trial": spent / max(1, len(trials)),
        "seconds": time.monotonic() - t0,
        "transitions": [t.model_dump(mode="json") for t in transitions],
        "verdicts": [x.result.verdict.value for x in trials],
    }
    Path(args.out).write_text(json.dumps(out, indent=1, default=str))
    print(f"final state {case.state.value}; {out['evidence_successes']}/{out['evidence_counted']} "
          f"counted evidence trials succeeded (reference {ref.p_alt:.3f}); {spent:,} tokens "
          f"(+{out['unconsumed_prefetched_tokens']:,} prefetched, unused); "
          f"wrote {args.out}")  # fmt: skip
    return 0


# ---- database-backed commands -------------------------------------------------------------------
def _setup(args: argparse.Namespace) -> tuple[Any, Any, Any]:
    from experience_engine.adapters.clock import SystemClock
    from experience_engine.adapters.db.controller_store import (
        SqlControllerPolicyStore,
        SqlDecisionLog,
    )
    from experience_engine.adapters.db.drift_store import SqlDriftLog
    from experience_engine.adapters.db.engine import InfraSettings, make_engine
    from experience_engine.adapters.db.revalidation_store import SqlRevalidationLog
    from experience_engine.controller.revalidation_service import RevalidationService

    engine = make_engine(InfraSettings.from_env().database_url)
    store = SqlControllerPolicyStore(engine)
    log = SqlDecisionLog(engine)
    rlog = SqlRevalidationLog(engine)
    svc = RevalidationService(
        lambda tenant, versions: log.outcome_records(tenant, policy_versions=versions),
        rlog,
        SqlDriftLog(engine),
        SystemClock().now,
    )
    return store, rlog, svc


def _definition(store: Any, args: argparse.Namespace) -> Any:
    rec = store.get_version(args.tenant, args.policy_id, args.version)
    if rec is None:
        raise SystemExit(f"{args.policy_id}/{args.version} not found for tenant {args.tenant}")
    return rec.definition


def _print(ts: list[Any]) -> None:
    for t in ts:
        frm = t.from_state.value if t.from_state else "-"
        print(
            f"  #{t.seq} {frm}->{t.to_state.value} {t.trigger.value} reason={t.reason.value} "
            f"episode={t.episode} cycle={t.cycle} n={t.n_trials} unverified={t.n_unverified} "
            f"tokens={t.tokens_cycle} applied={t.applied} {'; '.join(t.detail[:2])}"
        )


def _cmd_status(args: argparse.Namespace) -> int:
    store, rlog, _ = _setup(args)
    d = _definition(store, args)
    ts = rlog.transitions(args.tenant, args.policy_id, args.version)
    trials = rlog.trials(args.tenant, args.policy_id, args.version)
    rp = d.params.revalidation
    print(
        f"{d.policy_version}: revalidation "
        f"{(rp.strategy.value + ' (config ' + rp.config_hash()[:12] + ')') if rp else 'none'}"
        f"; {len(ts)} persisted transition(s), {len(trials)} trial(s) "
        f"({sum(1 for x in trials if not x.accepted)} rejected, "
        f"{sum(x.result.tokens for x in trials):,} tokens)"
    )
    last: dict[str, Any] = {}
    for t in ts:
        last[t.cell_key] = t
    for cell, t in sorted(last.items()):
        print(
            f"  {cell}: {t.to_state.value} (episode {t.episode}, cycle {t.cycle}, "
            f"{t.trigger.value})"
        )
    _print(ts)
    return 0


def _cmd_evaluate(args: argparse.Namespace) -> int:
    from experience_engine.controller.revalidation_service import RevalidationReplayMismatch

    store, _, svc = _setup(args)
    d = _definition(store, args)
    if d.params.revalidation is None:
        print("this policy version has no revalidation configuration")
        return 1
    try:
        res = svc.sync(d)
    except RevalidationReplayMismatch as exc:
        print(f"NOT reproducible: {exc}")
        return 2
    cells = 0 if res.overlay is None else len(res.overlay.cells)
    print(f"{d.policy_version}: synced from {res.n_observations} acted decisions; "
          f"{cells} cell(s) prior-disabled in the published overlay")  # fmt: skip
    return 0


def _cmd_replay(args: argparse.Namespace) -> int:
    from experience_engine.controller.revalidation_service import RevalidationReplayMismatch

    store, _, svc = _setup(args)
    d = _definition(store, args)
    try:
        res = svc.replay(d)
    except RevalidationReplayMismatch as exc:
        print(f"NOT reproducible: {exc}")
        return 1
    print(
        "persisted revalidation evidence reproduces exactly from "
        f"{res.n_observations} acted decisions; {len(res.new_transitions)} recomputed "
        "transition(s) not yet persisted"
    )
    return 0


def register(sub: Any) -> None:
    sim = sub.add_parser("reval-sim", help="the frozen M6.3 revalidation experiment (synthetic)")
    sim.add_argument("--scenarios", nargs="+", default=list(REVAL_SCENARIOS),
                     choices=list(REVAL_SCENARIOS))  # fmt: skip
    sim.add_argument("--seed-start", type=int, default=9000000)
    sim.add_argument("--seeds", type=int, default=100)
    sim.add_argument("--variants", nargs="+", default=list(REVAL_VARIANTS),
                     choices=list(REVAL_VARIANTS))  # fmt: skip
    sim.add_argument("--set", nargs="*", default=None, metavar="KEY=VALUE",
                     help="EXPLORATORY override of a RevalidationParams field")  # fmt: skip
    sim.add_argument("--drift-set", nargs="*", default=None, metavar="KEY=VALUE",
                     help="EXPLORATORY override of a DriftParams field")  # fmt: skip
    sim.add_argument("--horizon", type=int, default=None,
                     help="EXPLORATORY: stream length (default: the scenario's 800)")  # fmt: skip
    sim.add_argument("--workers", type=int, default=8)
    sim.add_argument("--json-out", required=True)
    sim.set_defaults(func=_cmd_sim)

    real = sub.add_parser(
        "reval-real", help="EXPLORATORY: one revalidation case with the real model"
    )
    real.add_argument("--prior", nargs="+", required=True, metavar="RESULTS")
    real.add_argument("--families", nargs="+", default=["timeout"])
    real.add_argument("--root", required=True)
    real.add_argument("--private-root", default=None)
    real.add_argument("--llm", default="vertex")
    real.add_argument("--tenant", default="bench")
    real.add_argument("--strategy", default="SEQUENTIAL", choices=[s.value for s in RevalStrategy])
    real.add_argument("--reason", default="STRUCTURAL_INVALIDATION",
                      choices=["STRUCTURAL_INVALIDATION", "PERFORMANCE_DEGRADATION",
                               "EVIDENCE_STALE"])  # fmt: skip
    real.add_argument("--max-trials", type=int, default=200)
    real.add_argument("--max-rounds", type=int, default=100)
    real.add_argument("--round-size", type=int, default=10)
    real.add_argument(
        "--workers",
        type=int,
        default=1,
        help="concurrent real executions per round (cost of unused ones is reported)",
    )
    real.add_argument("--confirm-trials", type=int, default=20)
    real.add_argument("--offset", type=int, default=0)
    real.add_argument("--max-tasks", type=int, default=0)
    real.add_argument("--out", required=True)
    real.set_defaults(func=_cmd_real)

    rep = sub.add_parser("reval-report", help="pre-registered acceptance and verdict")
    rep.add_argument("--results", required=True)
    rep.add_argument("--repro", default=None, help="a second run; compared byte for byte")
    rep.add_argument("--title", default="M6.3 revalidation")
    rep.add_argument("--out", required=True)
    rep.set_defaults(func=_cmd_report)

    for name, fn, helptext in (
        ("reval-status", _cmd_status, "persisted revalidation cases, trials and transitions"),
        ("reval-evaluate", _cmd_evaluate, "sync: persist what the observations imply (no trials)"),
        ("reval-replay", _cmd_replay, "verify persisted evidence against a recomputation"),
    ):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--tenant", default="bench")
        p.add_argument("--policy-id", default="adaptive")
        p.add_argument("--version", type=int, required=True)
        p.set_defaults(func=fn)


__all__ = ["RevalResult", "register", "result_from_json"]
