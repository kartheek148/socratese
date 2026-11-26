"""Decision features: the typed, self-contained input of one controller decision."""

import hashlib

from experience_engine.controller.feasibility import feasible_actions, is_protected
from experience_engine.domain.controller import (
    CandidateFeature,
    ControllerKind,
    ControllerPolicyDefinition,
    DecisionFeatures,
    EvidenceView,
    Risk,
    cell_key,
)
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
    bandit = definition.kind == ControllerKind.BANDIT
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
        if bandit and not protected and not mismatch
        else None,
    )


def build_features(
    ctx: ControllerInput, definition: ControllerPolicyDefinition, snapshot_hash: str
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
    )
