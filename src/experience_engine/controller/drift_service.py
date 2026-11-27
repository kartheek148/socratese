"""Learning-plane service around the drift monitor (M6.2): sync, audit, shadow assessment.

`sync` replays the monitor over the acted, verified decisions of one policy version, checks that
the transitions already persisted are exactly a prefix of the recomputation (otherwise the log or
the configuration changed and nothing is appended), persists the new ones and returns the overlay
the controller reads at build time. Execution never waits for it and never reads live tables.
"""

from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from experience_engine.controller.drift import (
    MonitorFleet,
    observation_of,
    parse_paired_key,
    references_from_snapshot,
)
from experience_engine.controller.drift import evaluate as evaluate_window
from experience_engine.domain.controller import ControllerPolicyDefinition, DecisionOutcomeRecord
from experience_engine.domain.drift import (
    Detector,
    DriftMode,
    DriftOverlay,
    DriftState,
    DriftTransition,
    DriftTrigger,
)
from experience_engine.ports.drift_log import DriftLog

OutcomeReader = Callable[[str, Sequence[str]], list[DecisionOutcomeRecord]]


class DriftReplayMismatch(RuntimeError):
    """The persisted transitions are not a prefix of the recomputed ones."""


@dataclass(frozen=True)
class SyncResult:
    overlay: DriftOverlay | None  # None unless the policy enforces the monitor
    new: list[DriftTransition] = field(default_factory=list)
    fleet: MonitorFleet | None = None
    n_observations: int = 0


def _by_cell(ts: Sequence[DriftTransition]) -> dict[str, list[DriftTransition]]:
    out: dict[str, list[DriftTransition]] = defaultdict(list)
    for t in ts:
        out[t.cell_key].append(t)
    return {k: sorted(v, key=lambda x: x.seq) for k, v in out.items()}


def verify_prefix(
    persisted: Sequence[DriftTransition], recomputed: Sequence[DriftTransition]
) -> list[DriftTransition]:
    """The recomputed transitions not yet persisted; raises if the log is not a prefix."""
    have, want = _by_cell(persisted), _by_cell(recomputed)
    new: list[DriftTransition] = []
    for cell, stored in have.items():
        mine = want.get(cell, [])
        for a, b in zip(stored, mine, strict=False):
            if a.signature() != b.signature() or a.config_hash != b.config_hash:
                raise DriftReplayMismatch(
                    f"{cell}: persisted seq {a.seq} {a.signature()} != recomputed {b.signature()}"
                )
        if len(stored) > len(mine):
            raise DriftReplayMismatch(f"{cell}: {len(stored)} persisted > {len(mine)} recomputed")
    for cell, mine in want.items():
        new += mine[len(have.get(cell, [])) :]
    return sorted(new, key=lambda t: (t.decision_id, t.seq))


class DriftService:
    def __init__(self, outcomes: OutcomeReader, log: DriftLog, now: Callable[[], datetime]):
        self._outcomes = outcomes
        self._log = log
        self._now = now

    def replay(self, definition: ControllerPolicyDefinition) -> SyncResult:
        """Recompute without persisting (verification and dry runs)."""
        dp = definition.params.drift
        if dp is None:
            return SyncResult(None)
        records = self._outcomes(definition.tenant_id, [definition.policy_version])
        fleet = MonitorFleet(
            references_from_snapshot(definition.snapshot), dp, definition.tenant_id
        )
        fleet.ingest(records)
        persisted = self._log.transitions(
            definition.tenant_id, definition.policy_id, definition.version
        )
        new = verify_prefix(persisted, fleet.transitions)
        overlay = fleet.overlay() if dp.mode == DriftMode.ENFORCE else None
        return SyncResult(overlay, new, fleet, sum(1 for r in records if r.acted))

    def sync(self, definition: ControllerPolicyDefinition) -> SyncResult:
        res = self.replay(definition)
        dp = definition.params.drift
        if dp is not None and res.new:
            self._log.append(
                definition.tenant_id,
                definition.policy_id,
                definition.version,
                res.new,
                self._now(),
                dp.model_dump(mode="json"),
            )
        return res


# ---- shadow assessment ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ShadowAssessment:
    """Hindsight check of the fallback a SHADOW-mode monitor would have recommended: in shadow the
    alternative kept running, so what happened *after* each recommendation is observed."""

    cell_key: str
    recommended_at: str  # decision id of the QUARANTINE_APPLIED transition
    n_after: int
    verdict: str  # CONFIRMED | UNCONFIRMED | PENDING
    measurements: dict[str, float]


def shadow_assessments(
    definition: ControllerPolicyDefinition,
    transitions: Sequence[DriftTransition],
    records: Sequence[DecisionOutcomeRecord],
) -> list[ShadowAssessment]:
    dp = definition.params.drift
    if dp is None:
        return []
    refs = {r.cell_key: r for r in references_from_snapshot(definition.snapshot)}
    window_params = dp.model_copy(update={"detector": Detector.WINDOW})
    out: list[ShadowAssessment] = []
    for t in transitions:
        if not (
            t.to_state == DriftState.QUARANTINED and t.trigger == DriftTrigger.QUARANTINE_APPLIED
        ):
            continue
        ref = refs.get(t.cell_key)
        if ref is None:
            continue
        family, risk, default, alt = parse_paired_key(t.cell_key)
        after = [
            observation_of(r)
            for r in records
            if r.acted
            and r.decision_id > t.decision_id
            and (r.family, r.risk_level, r.default_key, r.action) == (family, risk, default, alt)
            and r.known
        ]
        win = [(int(o.success), o.tokens, ref.compatible(o)) for o in after][-dp.window :]
        if len(win) < dp.min_samples:
            out.append(ShadowAssessment(t.cell_key, t.decision_id, len(after), "PENDING", {}))
            continue
        ev = evaluate_window(ref, window_params, win, 0.0, after[-1].at)
        verdict = "CONFIRMED" if ev.level == 2 else "UNCONFIRMED"
        out.append(
            ShadowAssessment(t.cell_key, t.decision_id, len(after), verdict, ev.measurements)
        )
    return out
