"""ee-ctl drift commands that read or write PostgreSQL (M6.2): evaluate, status, replay,
shadow-report and evidence inspection. Registered on the existing `ee-ctl` parser."""

import argparse
import json
from collections import defaultdict
from typing import Any

from experience_engine import RUNTIME_VERSION
from experience_engine.adapters.clock import SystemClock
from experience_engine.adapters.db.controller_store import SqlControllerPolicyStore, SqlDecisionLog
from experience_engine.adapters.db.drift_store import SqlDriftLog
from experience_engine.adapters.db.engine import InfraSettings, make_engine
from experience_engine.bench.environment import ENVIRONMENT_VERSION
from experience_engine.bench.verifier import SimulationVerifier
from experience_engine.controller.drift import parse_paired_key, reference_of
from experience_engine.controller.drift_service import (
    DriftReplayMismatch,
    DriftService,
    shadow_assessments,
)
from experience_engine.controller.evidence_quality import (
    ServingContext,
    assess_evidence,
    facts_from_source,
)
from experience_engine.domain.controller import ControllerPolicyDefinition
from experience_engine.domain.drift import DriftState


def _setup(args: argparse.Namespace) -> tuple[Any, SqlDriftLog, SqlDecisionLog, DriftService]:
    engine = make_engine(InfraSettings.from_env().database_url)
    store = SqlControllerPolicyStore(engine)
    log, dlog = SqlDecisionLog(engine), SqlDriftLog(engine)
    service = DriftService(
        lambda tenant, versions: log.outcome_records(tenant, policy_versions=versions),
        dlog,
        SystemClock().now,
    )
    return store, dlog, log, service


def _definition(store: Any, args: argparse.Namespace) -> ControllerPolicyDefinition:
    rec = store.get_version(args.tenant, args.policy_id, args.version)
    if rec is None:
        raise SystemExit(f"{args.policy_id}/{args.version} not found for tenant {args.tenant}")
    return rec.definition  # type: ignore[no-any-return]


def _print_transitions(ts: list[Any]) -> None:
    for t in ts:
        key = parse_paired_key(t.cell_key)
        print(
            f"  {key[0]}|{key[1]} #{t.seq} {t.from_state.value}->{t.to_state.value} "
            f"{t.trigger.value} effect={t.effect.value} mode={t.mode.value} "
            f"applied={t.applied} at={t.decision_id} n={t.n_window}"
        )


def _cmd_evaluate(args: argparse.Namespace) -> int:
    store, _, _, service = _setup(args)
    d = _definition(store, args)
    if d.params.drift is None:
        print("this policy version has no drift monitor")
        return 1
    try:
        res = service.sync(d)
    except DriftReplayMismatch as exc:
        print(f"NOT reproducible: {exc}")
        return 2
    print(
        f"{d.policy_version}: monitor {d.params.drift.mode.value} "
        f"({d.params.drift.detector.value}, config {d.params.drift.config_hash()[:12]}); "
        f"{res.n_observations} acted decisions replayed; {len(res.new)} new transition(s) persisted"
    )
    _print_transitions(res.new)
    if res.fleet is not None:
        for cell, st in sorted(res.fleet.states.items()):
            print(
                f"  state {cell}: {st.state.value} (alt={st.n_alt}, default={st.n_default}, "
                f"unknown={st.n_unknown}, episode={st.episode})"
            )
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    store, dlog, _, _ = _setup(args)
    d = _definition(store, args)
    ts = dlog.transitions(args.tenant, args.policy_id, args.version)
    last: dict[str, Any] = {}
    for t in ts:
        last[t.cell_key] = t
    print(f"{d.policy_version}: {len(ts)} persisted transition(s)")
    for cell in sorted(d.snapshot.paired):
        latest = last.get(cell)
        state = latest.to_state.value if latest else DriftState.NORMAL.value
        since = f" (since #{latest.seq}, {latest.trigger.value})" if latest else ""
        print(f"  {cell}: {state}{since}")
    _print_transitions(ts)
    return 0


def _cmd_replay(args: argparse.Namespace) -> int:
    store, _, _, service = _setup(args)
    d = _definition(store, args)
    try:
        res = service.replay(d)
    except DriftReplayMismatch as exc:
        print(f"NOT reproducible: {exc}")
        return 1
    print(
        f"persisted transitions reproduce exactly from {res.n_observations} acted decisions; "
        f"{len(res.new)} recomputed transition(s) not yet persisted"
    )
    return 0


def _cmd_shadow_report(args: argparse.Namespace) -> int:
    store, dlog, log, _ = _setup(args)
    d = _definition(store, args)
    ts = dlog.transitions(args.tenant, args.policy_id, args.version)
    records = log.outcome_records(args.tenant, policy_versions=[d.policy_version])
    rows = shadow_assessments(d, ts, records)
    tally: dict[str, int] = defaultdict(int)
    for r in rows:
        tally[r.verdict] += 1
        print(
            f"  {r.cell_key} recommended at {r.recommended_at}: {r.verdict} (n after {r.n_after})"
        )
    print(
        f"{d.policy_version} ({d.params.drift.mode.value if d.params.drift else 'no monitor'}): "
        f"{len(rows)} fallback recommendation(s): {dict(tally)}; "
        "UNCONFIRMED ones are false fallback recommendations in hindsight"
    )
    return 0


def _cmd_inspect(args: argparse.Namespace) -> int:
    store, dlog, _, _ = _setup(args)
    d = _definition(store, args)
    ev = d.params.evidence
    ts = dlog.transitions(args.tenant, args.policy_id, args.version)
    last = {t.cell_key: t.to_state.value for t in ts}
    ctx = ServingContext(
        tenant_id=args.tenant,
        runtime_version=RUNTIME_VERSION,
        verifier_version=SimulationVerifier().version,
        environment_version=ENVIRONMENT_VERSION,
        n_min_pairs=ev.n_min_pairs if ev else 30,
        max_age_days=d.params.drift.max_evidence_age_days if d.params.drift else None,
        now=SystemClock().now(),
        allow_version_mismatch=getattr(args, "allow_version_mismatch", False),
    )
    out = []
    for key, cell in sorted(d.snapshot.paired.items()):
        src = reference_of(key, cell, d.snapshot.prior_sources)
        source = next(
            (s for s in d.snapshot.prior_sources if s.runtime_version == src.runtime_version),
            d.snapshot.prior_sources[0],
        )
        a = assess_evidence(
            key,
            cell,
            facts_from_source(source),
            ctx,
            drift_state=last.get(key, "NORMAL"),
        )
        out.append(a.model_dump(mode="json"))
    print(json.dumps(out, indent=1, default=str))
    return 0 if all(o["eligible"] for o in out) else 1


def register(sub: Any) -> None:
    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--tenant", default="bench")
        p.add_argument("--policy-id", default="adaptive")
        p.add_argument("--version", type=int, required=True)

    for name, fn, helptext in (
        ("drift-evaluate", _cmd_evaluate, "replay the monitor, persist new transitions"),
        ("drift-status", _cmd_status, "persisted drift state and transitions of a version"),
        ("drift-replay", _cmd_replay, "verify persisted transitions against a recomputation"),
        ("drift-shadow-report", _cmd_shadow_report, "hindsight check of SHADOW recommendations"),
        ("evidence-inspect", _cmd_inspect, "evidence assessment of a stored policy's priors"),
    ):
        p = sub.add_parser(name, help=helptext)
        common(p)
        if name == "evidence-inspect":
            p.add_argument("--allow-version-mismatch", action="store_true")
        p.set_defaults(func=fn)
