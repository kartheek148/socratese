"""Decision features: the typed, self-contained input of one controller decision."""

import hashlib

from experience_engine.controller.feasibility import feasible_actions, is_protected
from experience_engine.domain.controller import (
    CandidateFeature,
    ControllerKind,
    ControllerPolicyDefinition,
    DecisionFeatures,
    EvidenceView,
    PairedView,
    Risk,
    cell_key,
    paired_key,
)
from experience_engine.domain.drift import DriftMode, DriftOverlay, DriftView
from experience_engine.execution.controller import ControllerInput


def explore_draw(decision_id: str, policy_version: str) -> float:
    """Deterministic uniform draw in [0, 1) from the decision id: reproducible, no RNG state."""
    digest = hashlib.sha256(f"{decision_id}|{policy_version}|explore".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def features_for(
    definition: ControllerPolicyDefinition,
    snapshot_hash: str,
    *,
    decision_id: str,
    tenant_id: str,
    family: str | None,
    risk: Risk,
    n_allowed_tools: int,
    actions: list[CandidateFeature],
    default_key: str,
    overlay: DriftOverlay | None = None,
) -> DecisionFeatures:
    protected = is_protected(risk, definition.params.learned_risk_levels)
    mismatch = tenant_id != definition.tenant_id
    evidence: list[EvidenceView] = []
    if not protected and not mismatch:  # protected / foreign contexts never read estimates
        for a in actions:
            cell = definition.snapshot.cells.get(cell_key(family, risk, a.key))
            if cell is not None:
                evidence.append(
                    EvidenceView(
                        key=a.key, n=cell.n, successes=cell.successes, token_sum=cell.token_sum
                    )
                )
    module = definition.params.evidence is not None
    paired: list[PairedView] = []
    if module and not protected and not mismatch:  # protected / foreign contexts never read priors
        for a in actions:
            if a.key == default_key:
                continue
            pcell = definition.snapshot.paired.get(paired_key(family, risk, default_key, a.key))
            if pcell is not None:
                paired.append(PairedView(key=a.key, **pcell.model_dump()))
    dp = definition.params.drift
    drift: list[DriftView] = []
    if (
        module
        and dp is not None
        and dp.mode == DriftMode.ENFORCE
        and overlay is not None
        and not protected
        and not mismatch
    ):
        for a in actions:
            status = overlay.cells.get(paired_key(family, risk, default_key, a.key))
            if a.key != default_key and status is not None:
                drift.append(
                    DriftView(
                        key=a.key,
                        state=status.state,
                        seq=status.seq,
                        probes_remaining=status.probes_remaining,
                    )
                )
    explores = definition.kind in (ControllerKind.BANDIT, ControllerKind.EVIDENCE_DRIVEN)
    draws = explores or dp is not None  # recovery probes need the reproducible draw
    return DecisionFeatures(
        controller_kind=definition.kind,
        task_family=family,
        risk_level=risk,
        n_allowed_tools=n_allowed_tools,
        tenant_id=tenant_id,
        actions=actions,
        default_key=default_key,
        evidence=evidence,
        snapshot_hash=snapshot_hash,
        calibration_ok=definition.snapshot.calibration_ok,
        n_verified_total=definition.snapshot.n_verified,
        protected=protected,
        tenant_mismatch=mismatch,
        u=explore_draw(decision_id, definition.policy_version)
        if draws and not protected and not mismatch
        else None,
        paired=paired,
        evidence_module=module,
        drift=drift,
    )


def build_features(
    ctx: ControllerInput,
    definition: ControllerPolicyDefinition,
    snapshot_hash: str,
    overlay: DriftOverlay | None = None,
) -> DecisionFeatures:
    fs = feasible_actions(ctx.match)
    return features_for(
        definition,
        snapshot_hash,
        decision_id=ctx.decision_id,
        tenant_id=ctx.task.tenant_id,
        family=ctx.task.task_family,
        risk=ctx.task.risk_level,
        n_allowed_tools=len(ctx.task.allowed_tools),
        actions=fs.actions,
        default_key=fs.default_key,
        overlay=overlay,
    )
