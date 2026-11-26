"""Tabular success/cost estimator built only from verified outcomes (learning plane).

Context = (task family, risk level); action = planning or one capability ref. UNKNOWN outcomes are
never counted as success or failure; unacted (shadow) decisions are never used; records of another
tenant are rejected rather than skipped.
"""

import hashlib
from collections.abc import Sequence

from experience_engine.controller.calibration import CalibrationGate, prequential_calibration
from experience_engine.domain.controller import (
    CellStats,
    ControllerParams,
    DecisionOutcomeRecord,
    EstimatorSnapshot,
    EvidenceView,
    TrainingProvenance,
    cell_key,
)


def p_hat(successes: int, n: int) -> float:
    """Posterior mean under a uniform Beta(1, 1) prior."""
    return (successes + 1) / (n + 2)


def cost_hat(stats: CellStats | EvidenceView | None, prior: float, weight: float) -> float:
    """Expected tokens per run: cell mean shrunk toward the pooled context mean."""
    if stats is None or stats.n == 0:
        return prior
    return (stats.token_sum + weight * prior) / (stats.n + weight)


def build_snapshot(
    records: Sequence[DecisionOutcomeRecord],
    tenant_id: str,
    params: ControllerParams | None = None,
    gate: CalibrationGate | None = None,
) -> EstimatorSnapshot:
    params = params or ControllerParams()
    for r in records:
        if r.tenant_id != tenant_id:
            raise ValueError("training records must belong to a single tenant")
    acted = [r for r in records if r.acted]
    known = [r for r in acted if r.known]
    cells: dict[str, list[int]] = {}
    for r in known:
        c = cells.setdefault(cell_key(r.family, r.risk_level, r.action), [0, 0, 0])
        c[0] += 1
        c[1] += int(r.success)
        c[2] += r.tokens
    digest = hashlib.sha256()
    for r in known:
        digest.update(f"{r.decision_id}|{r.action}|{r.status.value}|{r.tokens}\n".encode())
    provenance = TrainingProvenance(
        n_records=len(records),
        n_verified=len(known),
        n_unknown_excluded=len(acted) - len(known),
        n_unacted_excluded=len(records) - len(acted),
        first_decision_id=known[0].decision_id if known else None,
        last_decision_id=known[-1].decision_id if known else None,
        source_hash=digest.hexdigest(),
    )
    return EstimatorSnapshot(
        tenant_id=tenant_id,
        cells={
            k: CellStats(n=v[0], successes=v[1], token_sum=v[2]) for k, v in sorted(cells.items())
        },
        n_verified=len(known),
        calibration=prequential_calibration(known, params.cost_prior_weight, gate),
        provenance=provenance,
    )
