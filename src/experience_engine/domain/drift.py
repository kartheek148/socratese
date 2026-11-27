"""Drift-monitor contracts (pure data, M6.2).

The monitor watches decisions that depend on a paired validation prior and answers one question:
are recent verified outcomes inconsistent enough with the evidence behind the current decision that
the decision should stop being trusted? It never selects a new capability. Design and frozen
parameters: docs/experiments/evidence-reliability-design.md.
"""

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

DETECTOR_VERSION = "drift-monitor/1"


class _F(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class DriftState(StrEnum):
    NORMAL = "NORMAL"
    SUSPECT = "SUSPECT"
    DRIFTED = "DRIFTED"
    QUARANTINED = "QUARANTINED"
    RECOVERING = "RECOVERING"


# States in which the historical prior may not control a decision.
PRIOR_DISABLED = frozenset({DriftState.DRIFTED, DriftState.QUARANTINED, DriftState.RECOVERING})


class DriftTrigger(StrEnum):
    SUCCESS_DEGRADATION = "SUCCESS_DEGRADATION"
    COST_DEGRADATION = "COST_DEGRADATION"
    UTILITY_DEGRADATION = "UTILITY_DEGRADATION"
    CONTEXT_MISMATCH = "CONTEXT_MISMATCH"
    EVIDENCE_STALE = "EVIDENCE_STALE"
    SIGNAL_CLEARED = "SIGNAL_CLEARED"
    QUARANTINE_APPLIED = "QUARANTINE_APPLIED"
    QUARANTINE_ELAPSED = "QUARANTINE_ELAPSED"
    RECOVERY_CRITERIA_MET = "RECOVERY_CRITERIA_MET"
    RECOVERY_FAILED = "RECOVERY_FAILED"


class DriftEffect(StrEnum):
    MONITOR = "MONITOR"  # no change to decisions
    FALLBACK_TO_RULE = "FALLBACK_TO_RULE"  # the prior may not control the alternative
    PROBE_ALLOWED = "PROBE_ALLOWED"  # bounded fresh-evidence probes
    TRUST_RESTORED = "TRUST_RESTORED"


class DriftMode(StrEnum):
    SHADOW = "SHADOW"  # monitor and record; never alters a decision
    ENFORCE = "ENFORCE"  # the state restricts decisions (needs a promoted policy version)


class Detector(StrEnum):
    WINDOW = "WINDOW"  # confidence-aware sliding-window comparison
    CUSUM = "CUSUM"  # Bernoulli CUSUM on success; window rules for the other channels


class DriftParams(_F):
    """Monitor configuration. Part of the immutable policy definition (and its hash)."""

    detector: Detector = Detector.WINDOW
    mode: DriftMode = DriftMode.SHADOW
    window: int = Field(default=40, ge=5)
    min_samples: int = Field(default=20, ge=5)
    suspect_confidence: float = Field(default=0.90, gt=0.5, lt=1.0)
    confidence: float = Field(default=0.995, gt=0.5, lt=1.0)
    success_tolerance: float = Field(default=0.10, gt=0.0, lt=1.0)
    cost_tolerance: float = Field(default=0.30, gt=0.0)
    utility_tolerance: float = Field(default=0.30, gt=0.0)
    persistence: int = Field(default=10, ge=1)
    clear_after: int = Field(default=10, ge=1)
    mismatch_min: int = Field(default=5, ge=1)
    max_evidence_age_days: float | None = Field(default=None, gt=0.0)
    min_quarantine: int = Field(default=50, ge=0)
    recovery_threshold: float = Field(default=0.05, ge=0.0, lt=1.0)
    recovery_confidence: float = Field(default=0.80, gt=0.5, lt=1.0)
    min_recovery_samples: int = Field(default=30, ge=5)
    recovery_persistence: int = Field(default=3, ge=1)
    probe_rate: float = Field(default=0.25, gt=0.0, le=0.5)
    probe_budget: int = Field(default=150, ge=1)
    max_episodes: int = Field(default=3, ge=1)
    cusum_shift: float = Field(default=0.15, gt=0.0, lt=0.9)
    cusum_threshold: float = Field(default=6.0, gt=0.0)

    @model_validator(mode="after")
    def _hysteresis(self) -> "DriftParams":
        if self.recovery_threshold >= self.success_tolerance:
            raise ValueError("recovery_threshold must be stricter than success_tolerance")
        if self.min_recovery_samples > self.window:
            raise ValueError("min_recovery_samples cannot exceed the window")
        if self.min_samples > self.window:
            raise ValueError("min_samples cannot exceed the window")
        return self

    def config_hash(self) -> str:
        blob = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()


class DriftView(_F):
    """What a decision read from the drift overlay for one alternative (so it replays)."""

    key: str  # the alternative's action key
    state: DriftState
    seq: int = Field(ge=0)  # sequence number of the transition that produced the state
    probes_remaining: int = Field(default=0, ge=0)


class DriftCellStatus(_F):
    state: DriftState
    seq: int = Field(ge=0)
    probes_remaining: int = Field(default=0, ge=0)


class DriftOverlay(_F):
    """The monitor's published state per cell, loaded at controller build (never per decision)."""

    cells: dict[str, DriftCellStatus] = Field(default_factory=dict)

    def overlay_hash(self) -> str:
        blob = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()


class DriftTransition(_F):
    """One audited state change of one evidence cell."""

    cell_key: str
    seq: int = Field(ge=1)
    from_state: DriftState
    to_state: DriftState
    trigger: DriftTrigger
    effect: DriftEffect
    measurements: dict[str, float] = Field(default_factory=dict)
    n_window: int = Field(ge=0)
    decision_id: str
    observed_at: datetime | None = None
    detector_version: str = DETECTOR_VERSION
    config_hash: str
    mode: DriftMode = DriftMode.SHADOW

    @property
    def applied(self) -> bool:
        return self.mode == DriftMode.ENFORCE

    def signature(self) -> tuple[str, int, str, str, str, str]:
        return (
            self.cell_key,
            self.seq,
            self.from_state.value,
            self.to_state.value,
            self.trigger.value,
            self.decision_id,
        )


def effect_of(to_state: DriftState, from_state: DriftState) -> DriftEffect:
    if to_state in (DriftState.DRIFTED, DriftState.QUARANTINED):
        return DriftEffect.FALLBACK_TO_RULE
    if to_state == DriftState.RECOVERING:
        return DriftEffect.PROBE_ALLOWED
    if to_state == DriftState.NORMAL and from_state == DriftState.RECOVERING:
        return DriftEffect.TRUST_RESTORED
    return DriftEffect.MONITOR


def drift_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
