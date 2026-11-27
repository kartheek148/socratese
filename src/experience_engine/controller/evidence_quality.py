"""Evidence assessment for paired priors (M6.2, pure).

Every gate is explicit and independent; a failed gate adds a reason and makes the evidence
ineligible for a *production* prior. Research use (result files, stress fixtures) stays possible
and is labelled. An explicit override is recorded in the assessment and never weakens another gate.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from experience_engine.controller.drift import parse_paired_key
from experience_engine.domain.controller import PairedCell, PairedSource
from experience_engine.domain.evidence import EvaluationRecord, EvidenceAssessment, EvidenceKind
from experience_engine.domain.stats import newcombe_paired_bounds

HELD_OUT_PARTITIONS = frozenset({"HELD_OUT", "TRANSFER", "VALIDATION", "HIDDEN_TRANSFER"})
TEST_DOUBLE_PROVIDERS = frozenset({"fake"})
MAX_UNKNOWN_FRACTION = 0.20


@dataclass(frozen=True)
class EvidenceFacts:
    """What is known about one evidence source (from a stored evaluation or a stored prior)."""

    kind: str | None
    tenant_id: str | None
    risk_class: str | None
    splits: Sequence[str]
    runtime_version: str | None
    verifier_version: str | None
    environment_version: str | None
    capabilities: Sequence[str]
    n_unknown: int
    n_tied: int
    n_missing_cost: int | None
    n_duplicate_rows: int | None = None
    evaluation_id: str | None = None
    report_sha256: str | None = None
    config_hash: str | None = None
    verdict: str | None = None  # promotion evaluations only
    model_provider: str | None = None
    recorded_at: datetime | None = None
    capability_hash: str | None = None
    recorded_ineligible: bool = False  # a stored source already marked ineligible at build time
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ServingContext:
    """What the evidence will be used for."""

    tenant_id: str
    runtime_version: str
    verifier_version: str
    environment_version: str | None = None
    n_min_pairs: int = 30
    max_age_days: float | None = None
    now: datetime | None = None
    allow_version_mismatch: bool = False


def facts_from_source(source: PairedSource, model_provider: str | None = None) -> EvidenceFacts:
    return EvidenceFacts(
        kind=source.evidence_kind,
        tenant_id=source.tenant_id,
        risk_class=source.risk_class,
        splits=source.splits,
        runtime_version=source.runtime_version,
        verifier_version=source.verifier_version,
        environment_version=source.environment_version,
        capabilities=source.capabilities,
        n_unknown=source.n_unknown_dropped,
        n_tied=source.n_tied_dropped,
        n_missing_cost=source.n_missing_cost,
        evaluation_id=source.evaluation_id,
        report_sha256=source.report_sha256,
        config_hash=source.evaluation_config_hash,
        model_provider=model_provider
        or ("fake" if source.model.startswith("heuristic-fake") else None),
        recorded_at=source.recorded_at,
        recorded_ineligible=source.production_eligible is False,
    )


def assess_evidence(
    cell_key: str,
    cell: PairedCell,
    facts: EvidenceFacts,
    ctx: ServingContext,
    *,
    drift_state: str = "NORMAL",
    seen_evaluations: frozenset[str] = frozenset(),
) -> EvidenceAssessment:
    family, risk, default, alt = parse_paired_key(cell_key)
    reasons: list[str] = []
    overrides: list[str] = []
    compat: dict[str, bool] = {}

    def gate(name: str, ok: bool, why: str) -> None:
        compat[name] = ok
        if not ok:
            reasons.append(why)

    gate(
        "kind",
        facts.kind == EvidenceKind.PROMOTION_EVALUATION.value,
        f"evidence kind {facts.kind!r} is research-only (a production prior needs a "
        "PROMOTION_EVALUATION)",
    )
    gate(
        "tenant",
        facts.tenant_id == ctx.tenant_id,
        f"evidence tenant {facts.tenant_id!r} is not the training tenant {ctx.tenant_id!r}",
    )
    gate(
        "risk",
        facts.risk_class == risk,
        f"evidence risk class {facts.risk_class!r} differs from the cell's {risk!r}",
    )
    gate(
        "partition",
        bool(facts.splits) and set(facts.splits) <= HELD_OUT_PARTITIONS,
        f"evidence partitions {sorted(facts.splits)} are not held-out only",
    )
    for name, theirs, ours in (
        ("runtime", facts.runtime_version, ctx.runtime_version),
        ("verifier", facts.verifier_version, ctx.verifier_version),
        ("environment", facts.environment_version, ctx.environment_version),
    ):
        if ours is None:
            compat[name] = True
            continue
        ok = theirs == ours
        if not ok and ctx.allow_version_mismatch:
            overrides.append(f"allow-version-mismatch: {name} {theirs!r} serving {ours!r}")
            ok = True
        gate(name, ok, f"{name} version {theirs!r} differs from the serving {ours!r}")
    refs = {r for r in (_ref(default), _ref(alt)) if r}
    gate(
        "capability_version",
        bool(refs) and bool(refs & set(facts.capabilities)),
        f"capability version(s) {sorted(refs)} are not those the evidence covers "
        f"{sorted(facts.capabilities)}",
    )
    gate(
        "sample_size",
        cell.n >= ctx.n_min_pairs,
        f"{cell.n} verified pairs < the required {ctx.n_min_pairs}",
    )
    seen = cell.n + facts.n_unknown + facts.n_tied
    frac = (facts.n_unknown + facts.n_tied) / seen if seen else 1.0
    gate(
        "outcome_verification",
        frac <= MAX_UNKNOWN_FRACTION,
        f"{frac:.0%} of the paired tasks were UNKNOWN or tied (> {MAX_UNKNOWN_FRACTION:.0%})",
    )
    gate(
        "cost_measurement",
        facts.n_missing_cost == 0,
        "cost measurement is incomplete or not recorded"
        if facts.n_missing_cost is None
        else f"{facts.n_missing_cost} pair(s) lack a token measurement",
    )
    gate(
        "no_duplicates",
        (facts.n_duplicate_rows in (None, 0))
        and (facts.evaluation_id is None or facts.evaluation_id not in seen_evaluations),
        "duplicate evidence (repeated rows or an evaluation counted twice)",
    )
    if facts.verdict is not None:
        gate("validated", facts.verdict == "PASS", f"the evaluation verdict was {facts.verdict}")
    gate(
        "not_synthetic",
        facts.model_provider not in TEST_DOUBLE_PROVIDERS and not facts.recorded_ineligible,
        "evidence is synthetic (test-double model) or was recorded as ineligible",
    )
    age = None
    fresh: bool | None = None
    if facts.recorded_at is not None and ctx.now is not None:
        age = (ctx.now - facts.recorded_at).total_seconds() / 86400.0
    if ctx.max_age_days is not None:
        fresh = age is not None and age <= ctx.max_age_days
        gate("freshness", bool(fresh), f"evidence age {age} days exceeds {ctx.max_age_days}")
    gate(
        "drift_state",
        drift_state in ("NORMAL", "SUSPECT"),
        f"the evidence cell is {drift_state}",
    )
    lo = hi = 0.0
    if cell.n:
        lo, hi = newcombe_paired_bounds(
            cell.both, cell.alt_only, cell.default_only, cell.neither, 0.95
        )
    return EvidenceAssessment(
        cell_key=cell_key,
        eligible=not reasons,
        reasons=reasons,
        verified_pair_count=cell.n,
        n_unknown_dropped=facts.n_unknown,
        n_tied_dropped=facts.n_tied,
        confidence={"lower": lo, "upper": hi, "level": 0.95, "width": hi - lo},
        compatibility=compat,
        freshness={"age_days": age, "max_age_days": ctx.max_age_days, "fresh": fresh},
        drift_state=drift_state,
        cost_measurement={
            "n_missing": float(facts.n_missing_cost or 0),
            "complete": 1.0 if facts.n_missing_cost == 0 else 0.0,
        },
        provenance={
            "kind": facts.kind,
            "evaluation_id": facts.evaluation_id,
            "report_sha256": facts.report_sha256,
            "config_hash": facts.config_hash,
            "capability_hash": facts.capability_hash,
            "family": family,
        },
        overrides=overrides,
    )


def _ref(key: str) -> str | None:
    _, sep, ref = key.partition(":")
    return ref if sep else None


# ---- stored evaluations -> paired evidence -----------------------------------------------------
def source_and_cell(rec: EvaluationRecord) -> tuple[str, PairedCell, PairedSource, EvidenceFacts]:
    """(cell key, 2x2 cell, provenance, facts) of one evaluation's serving evidence."""
    from experience_engine.domain.controller import PLANNING_KEY, action_key, paired_key
    from experience_engine.domain.decision import Strategy

    ev = rec.serving
    if ev is None:
        raise ValueError("the evaluation recorded no serving (final-outcome) evidence")
    cell = PairedCell(
        n=ev.n,
        both=ev.both,
        default_only=ev.default_only,
        alt_only=ev.alt_only,
        neither=ev.neither,
        default_tokens=ev.default_tokens,
        alt_tokens=ev.alt_tokens,
    )
    default = action_key(Strategy.DIRECT_CAPABILITY, ev.capability_ref)
    key = paired_key(ev.family, ev.risk_class, default, PLANNING_KEY)
    provider = str(rec.model_settings.get("provider", "")) or None
    synthetic = provider in TEST_DOUBLE_PROVIDERS
    source = PairedSource(
        name=f"evaluation:{rec.evaluation_id}",
        arms=["A_BASELINE", "E_SERVING"],
        splits=list(ev.suites),
        dataset_hashes={
            s: d.get("content_hash", "")
            for s, d in rec.dataset_versions.items()
            if s in ("VALIDATION", "HIDDEN_TRANSFER")
        },
        model=str(rec.model_settings.get("model", "")),
        runtime_version=rec.runtime_version,
        verifier_version=rec.verifier_version,
        n_pairs=ev.n,
        n_unknown_dropped=ev.n_unknown_dropped,
        n_tied_dropped=ev.n_tied_dropped,
        tasks_sha256=ev.tasks_sha256,
        validation_tokens=ev.default_tokens + ev.alt_tokens,
        capabilities=[ev.capability_ref],
        evidence_kind=EvidenceKind.PROMOTION_EVALUATION.value,
        production_eligible=None if not synthetic else False,
        tenant_id=rec.tenant_id,
        risk_class=ev.risk_class,
        evaluation_id=rec.evaluation_id,
        report_sha256=rec.report_sha256,
        evaluation_config_hash=str(rec.metrics.get("evaluation_config_hash") or ""),
        environment_version=rec.environment_version,
        recorded_at=rec.completed_at,
        n_missing_cost=ev.n_missing_cost,
    )
    facts = EvidenceFacts(
        kind=EvidenceKind.PROMOTION_EVALUATION.value,
        tenant_id=rec.tenant_id,
        risk_class=ev.risk_class,
        splits=ev.suites,
        runtime_version=rec.runtime_version,
        verifier_version=rec.verifier_version,
        environment_version=rec.environment_version,
        capabilities=[ev.capability_ref],
        n_unknown=ev.n_unknown_dropped,
        n_tied=ev.n_tied_dropped,
        n_missing_cost=ev.n_missing_cost,
        n_duplicate_rows=ev.n_duplicate_rows,
        evaluation_id=rec.evaluation_id,
        report_sha256=rec.report_sha256,
        config_hash=source.evaluation_config_hash,
        verdict=rec.verdict,
        model_provider=provider,
        recorded_at=rec.completed_at,
        capability_hash=ev.capability_hash,
    )
    return key, cell, source, facts
