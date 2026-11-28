"""Recovery evidence gates (M6.4 Phase 1, pure).

Eleven deterministic gates decide whether the evidence behind a recovery is *eligible*. They are
fail-closed: unavailable provenance is a failed gate, never a permissive default; an empty
evidence set fails every evidence gate instead of passing vacuously. All gates are always
evaluated (no short-circuit), so a report names every failure. The result is a persisted
`GateReport`, derived from the evidence and the policy alone, so a replay reproduces it exactly.
Nothing here executes a model or reads a clock: the decision time is an input.
"""

import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from pydantic import ValidationError

from experience_engine.domain.recovery import (
    EVIDENCE_CLASSES,
    GATES_VERSION,
    GateId,
    GateReport,
    GateResult,
    RecoveryPolicy,
    VerificationStatus,
)
from experience_engine.domain.revalidation import (
    ExecutorFacts,
    RevalTrial,
    TrialStage,
)

_MAX_REASONS = 5
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_UNSTAMPED = datetime.min + timedelta(days=1)  # the executor placeholder `datetime.min`


class RecoveryGateViolation(RuntimeError):
    """A recovery was about to be applied or persisted without a passing gate evaluation."""


@dataclass(frozen=True)
class GateContext:
    """What the evidence is evaluated against."""

    policy: RecoveryPolicy
    tenant_id: str
    cell_key: str
    episode: int
    cycle: int
    family: str | None
    risk: str
    alt_key: str
    ref_runtime: str | None
    trigger_at: datetime | None  # the evidence must be newer than the evidence that quarantined
    decision_at: datetime | None  # when the decision is taken (an input, never the wall clock)
    config_hash: str
    confirm_required: int


# ---- value checks shared by the trial gates and the executor preflight --------------------------
def executor_issues(policy: RecoveryPolicy, executor_id: str | None) -> list[str]:
    if executor_id is None:
        return ["executor not attested"]
    if executor_id not in policy.accepted_executors:
        return [f"executor {executor_id!r} is not an accepted executor"]
    return []


def runtime_issues(
    policy: RecoveryPolicy,
    ref_runtime: str | None,
    runtime: str | None,
    model_id: str | None,
    model_version: str | None,
) -> list[str]:
    out: list[str] = []
    if runtime is None:
        out.append("runtime version not attested")
    elif runtime != ref_runtime and runtime not in policy.validated_runtimes:
        out.append(f"runtime {runtime!r} is neither the reference runtime nor validated")
    if model_id is None:
        out.append("model not attested")
    if model_version is None:
        out.append("model version not attested")
    if (
        model_id is not None
        and model_version is not None
        and model_version not in policy.accepted_models.get(model_id, ())
    ):
        out.append(f"model {model_id!r} version {model_version!r} is not accepted")
    return out


def tool_issues(policy: RecoveryPolicy, tools: dict[str, str] | None) -> list[str]:
    if tools is None:
        return ["tools not attested"]
    out: list[str] = []
    for tool, version in sorted(tools.items()):
        if version not in policy.accepted_tools.get(tool, ()):
            out.append(f"tool {tool!r} version {version!r} is not accepted")
    return out


def verifier_issues(
    policy: RecoveryPolicy,
    verifier_id: str | None,
    verifier_version: str | None,
    independent: bool | None,
    executor_id: str | None,
    model_id: str | None,
) -> list[str]:
    out: list[str] = []
    if verifier_id is None:
        out.append("verifier not attested")
    if verifier_version is None:
        out.append("verifier version not attested")
    if (
        verifier_id is not None
        and verifier_version is not None
        and verifier_version not in policy.accepted_verifiers.get(verifier_id, ())
    ):
        out.append(f"verifier {verifier_id!r} version {verifier_version!r} is not accepted")
    if independent is not True:
        out.append("verifier independence not attested")
    if verifier_id is not None and verifier_id in (executor_id, model_id):
        out.append("the verifier is not independent of the executor or model")
    return out


def preflight(policy: RecoveryPolicy, ref_runtime: str | None, facts: ExecutorFacts) -> list[str]:
    """Static compatibility of the executor itself, checked before any tokens are spent. The
    sweep over the recorded trials remains authoritative."""
    issues: list[str] = []
    for gate, found in (
        (GateId.EXECUTOR_COMPATIBILITY, executor_issues(policy, facts.executor_id)),
        (
            GateId.RUNTIME_COMPATIBILITY,
            runtime_issues(
                policy, ref_runtime, facts.runtime_version, facts.model_id, facts.model_version
            ),
        ),
        (GateId.TOOL_COMPATIBILITY, tool_issues(policy, facts.tool_versions)),
        (
            GateId.VERIFIER_INTEGRITY,
            verifier_issues(
                policy,
                facts.verifier_id,
                facts.verifier_version,
                facts.verifier_independent,
                facts.executor_id,
                facts.model_id,
            ),
        ),
    ):
        issues += [f"gate:{gate.value}: {x}" for x in found]
    return issues


# ---- timestamps ---------------------------------------------------------------------------------
def _comparable(a: datetime, b: datetime) -> bool:
    return (a.tzinfo is None) == (b.tzinfo is None)


def _unstamped(at: datetime) -> bool:
    return at.replace(tzinfo=None) <= _UNSTAMPED


# ---- evaluation ---------------------------------------------------------------------------------
class _Gate:
    def __init__(self, gate: GateId, checked: int):
        self.gate = gate
        self.checked = checked
        self.failed: set[str] = set()
        self.reasons: set[str] = set()

    def fail(self, ident: str | None, reason: str) -> None:
        if ident is not None:
            self.failed.add(ident)
        self.reasons.add(reason)

    def result(self) -> GateResult:
        return GateResult(
            gate=self.gate,
            passed=not self.failed and not self.reasons,
            checked=self.checked,
            failed_evidence=sorted(self.failed),
            reasons=sorted(self.reasons)[:_MAX_REASONS],
        )


def _order(t: RevalTrial) -> tuple[int, int]:
    return (0 if t.key.stage == TrialStage.EVIDENCE else 1, t.key.ordinal)


def _constant(g: _Gate, label: str, values: Iterable[object]) -> None:
    if len(set(values)) > 1:
        g.reasons.add(f"{label} changed within the evidence set")


def evaluate_gates(ctx: GateContext, trials: Sequence[RevalTrial]) -> GateReport:
    """Evaluate all eleven gates over the evidence of one cycle (evidence and confirmation
    trials). Deterministic: the same trials, context and decision time give the same report."""
    items = sorted(trials, key=_order)
    n = len(items)
    pol = ctx.policy
    gates = {g: _Gate(g, n) for g in GateId}
    if not items:
        for gate in GateId:
            gates[gate].reasons.add("no evidence to evaluate")

    # 1 evidence presence
    g = gates[GateId.EVIDENCE_PRESENCE]
    evid = [
        t for t in items if t.key.stage == TrialStage.EVIDENCE and t.accepted and t.result.counted
    ]
    conf = [
        t
        for t in items
        if t.key.stage == TrialStage.CONFIRMATION and t.accepted and t.result.counted
    ]
    if items and not evid:
        g.fail(None, "no counted evidence-stage trial")
    if items and len(conf) < ctx.confirm_required:
        g.fail(None, f"{len(conf)} counted confirmation trials < {ctx.confirm_required} required")
    for t in items:
        if not (t.accepted and t.result.counted):
            g.fail(t.key.ident(), "a rejected or non-evidence trial is in the evidence set")

    for t in items:
        ident, r, p = t.key.ident(), t.result, t.result.provenance

        # 2 capability identity
        if r.capability_ref is None:
            gates[GateId.CAPABILITY_IDENTITY].fail(ident, "capability not attested")
        elif r.capability_ref != ctx.alt_key:
            gates[GateId.CAPABILITY_IDENTITY].fail(
                ident, f"evidence belongs to {r.capability_ref!r}, not {ctx.alt_key!r}"
            )

        # 3 context isolation
        g = gates[GateId.CONTEXT_ISOLATION]
        if not ctx.tenant_id:
            g.fail(ident, "the recovery scope has no tenant")
        elif r.tenant_id is None:
            g.fail(ident, "tenant not attested")
        elif r.tenant_id != ctx.tenant_id:
            g.fail(ident, f"tenant {r.tenant_id!r} is outside the recovery scope")
        if ctx.family is not None:
            if p.task_family is None:
                g.fail(ident, "task family not attested")
            elif p.task_family != ctx.family:
                g.fail(ident, f"task family {p.task_family!r} is outside the recovery scope")
        if p.risk_level is None:
            g.fail(ident, "risk class not attested")
        elif p.risk_level != ctx.risk:
            g.fail(ident, f"risk class {p.risk_level!r} is outside the recovery scope")

        # 4 executor, 5 model and runtime, 6 tools, 7 verifier
        for why in executor_issues(pol, p.executor_id):
            gates[GateId.EXECUTOR_COMPATIBILITY].fail(ident, why)
        for why in runtime_issues(
            pol, ctx.ref_runtime, r.runtime_version, p.model_id, p.model_version
        ):
            gates[GateId.RUNTIME_COMPATIBILITY].fail(ident, why)
        for why in tool_issues(pol, p.tool_versions):
            gates[GateId.TOOL_COMPATIBILITY].fail(ident, why)
        for why in verifier_issues(
            pol,
            p.verifier_id,
            r.verifier_version,
            p.verifier_independent,
            p.executor_id,
            p.model_id,
        ):
            gates[GateId.VERIFIER_INTEGRITY].fail(ident, why)

        # 8 freshness (inclusive window; a future timestamp is not supported)
        g = gates[GateId.EVIDENCE_FRESHNESS]
        if _unstamped(r.observed_at):
            g.fail(ident, "timestamp missing")
        elif ctx.decision_at is None:
            g.fail(ident, "decision time unknown")
        elif not _comparable(r.observed_at, ctx.decision_at):
            g.fail(ident, "timestamp not comparable with the decision time")
        else:
            age = (ctx.decision_at - r.observed_at).total_seconds() / 3600.0
            if age < 0:
                g.fail(ident, "timestamp is in the future")
            elif age > pol.evidence_window_hours:
                g.fail(ident, f"older than the {pol.evidence_window_hours:g} h window")

        # 9 verified outcome
        g = gates[GateId.VERIFIED_OUTCOME]
        if r.outcome_class not in EVIDENCE_CLASSES:
            g.fail(ident, f"{r.outcome_class.value} is not an evidence outcome")
        if not r.attributed:
            g.fail(ident, "timeout origin not attributed")
        if p.verification_status is None:
            g.fail(ident, "verification status not attested")
        elif p.verification_status != VerificationStatus.VERIFIED:
            g.fail(ident, f"verification status {p.verification_status.value}")

        # 10 lineage (per trial)
        g = gates[GateId.LINEAGE_INTEGRITY]
        if (t.key.cell_key, t.key.episode, t.key.cycle) != (ctx.cell_key, ctx.episode, ctx.cycle):
            g.fail(ident, "the trial does not belong to this case and cycle")
        if not r.task_ref:
            g.fail(ident, "task reference missing")
        if p.execution_id is None:
            g.fail(ident, "execution id not attested")
        if p.evidence_source is None:
            g.fail(ident, "evidence source not attested")
        elif p.evidence_source not in pol.accepted_sources:
            g.fail(ident, f"evidence source {p.evidence_source!r} is not accepted")
        if p.pool_id is None:
            g.fail(ident, "task pool not attested")
        if ctx.trigger_at is None:
            g.fail(ident, "the quarantining evidence time is unknown")
        elif _unstamped(r.observed_at):
            g.fail(ident, "timestamp missing")
        elif not _comparable(r.observed_at, ctx.trigger_at):
            g.fail(ident, "timestamp not comparable with the quarantine time")
        elif r.observed_at <= ctx.trigger_at:
            g.fail(ident, "not newer than the evidence that triggered the quarantine")

    # constancy and uniqueness over the whole set
    if items:
        results = [t.result for t in items]
        _constant(
            gates[GateId.EXECUTOR_COMPATIBILITY],
            "executor",
            (x.provenance.executor_id for x in results),
        )
        g = gates[GateId.RUNTIME_COMPATIBILITY]
        _constant(g, "runtime version", (x.runtime_version for x in results))
        _constant(g, "model", (x.provenance.model_id for x in results))
        _constant(g, "model version", (x.provenance.model_version for x in results))
        _constant(
            gates[GateId.VERIFIER_INTEGRITY],
            "verifier",
            ((x.provenance.verifier_id, x.verifier_version) for x in results),
        )
        g = gates[GateId.LINEAGE_INTEGRITY]
        for label, values in (
            ("trial", [t.key.ident() for t in items]),
            ("task", [t.result.task_ref for t in items]),
            (
                "execution",
                [x.provenance.execution_id for x in results if x.provenance.execution_id],
            ),
        ):
            dup = sorted(k for k, c in Counter(values).items() if c > 1)
            if dup:
                g.fail(None, f"duplicate {label} in the evidence set")
                for t in items:
                    own = {"trial": t.key.ident(), "task": t.result.task_ref,
                           "execution": t.result.provenance.execution_id}[label]  # fmt: skip
                    if own in dup:
                        g.failed.add(t.key.ident())

    # 11 policy integrity
    g = gates[GateId.POLICY_INTEGRITY]
    if not pol.gates_version:
        g.fail(None, "gate policy version missing")
    elif pol.gates_version != GATES_VERSION:
        g.fail(None, f"unsupported gate policy version {pol.gates_version!r}")
    if not _SHA256.match(ctx.config_hash or ""):
        g.fail(None, "revalidation configuration hash missing")

    results_ = [gates[x].result() for x in GateId]
    failed = [r.gate.value for r in results_ if not r.passed]
    return GateReport(
        eligible=not failed and bool(items) and bool(pol.gates_version),
        policy_version=pol.gates_version,
        failed_gates=failed,
        passed_gates=[r.gate.value for r in results_ if r.passed],
        evidence_ids=[t.key.ident() for t in items],
        results=results_,
        evaluated_at=ctx.decision_at,
        config_hash=ctx.config_hash,
        tenant_id=ctx.tenant_id,
        cell_key=ctx.cell_key,
        episode=ctx.episode,
        cycle=ctx.cycle,
    )


# ---- admissibility of a persisted decision ------------------------------------------------------
def admissibility_problems(
    policy: RecoveryPolicy | None, config_hash: str, payload: dict[str, Any]
) -> list[str]:
    """Why a persisted gate payload does not authorise a recovery (empty = it does). A populated
    payload is not enough: it must parse, derive `eligible` from all eleven passing gates, name
    its evidence and policy version, and have been evaluated under this configuration."""
    if policy is None:
        return []  # M6.3 semantics: no gate evaluation exists to require
    if not payload:
        return ["no gate evaluation"]
    try:
        report = GateReport.model_validate(payload)
    except ValidationError as exc:
        return [f"malformed gate report: {exc.errors()[0]['msg']}"]
    problems: list[str] = []
    if not report.eligible:
        problems.append(f"gates failed: {', '.join(report.failed_gates) or 'incomplete report'}")
    if report.policy_version != policy.gates_version:
        problems.append("gate report was produced under another gate policy version")
    if report.config_hash != config_hash:
        problems.append("gate report was produced under another configuration")
    if not report.evidence_ids:
        problems.append("gate report names no evidence")
    return problems
