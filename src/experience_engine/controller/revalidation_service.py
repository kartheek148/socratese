"""Learning-plane service around controlled revalidation (M6.3): sync, run, replay, audit.

`sync` is what the controller build calls: it recomputes the fleet from the ordered outcomes and
the *persisted* trials only (it never executes a trial), checks that everything already persisted
is exactly what the recomputation gives, persists what the observations newly imply (new cases,
monitor transitions) and returns the overlay. `run` is the background worker: the same, then one
live round with an executor, persisting the new trials and transitions atomically. Execution never
waits for either, and neither can restore a cell without persisted, replayable evidence.
"""

from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from experience_engine.controller.drift import references_from_snapshot
from experience_engine.controller.drift_service import (
    DriftService,
    OutcomeReader,
    SyncResult,
    verify_prefix,
)
from experience_engine.controller.recovery_gates import (
    RecoveryGateViolation,
    admissibility_problems,
)
from experience_engine.controller.revalidation import (
    RevalidationFleet,
    TrialSource,
    replay_revalidation,
)
from experience_engine.domain.controller import ControllerPolicyDefinition, DecisionOutcomeRecord
from experience_engine.domain.drift import DriftMode, DriftOverlay
from experience_engine.domain.revalidation import RevalState, RevalTransition, RevalTrial
from experience_engine.ports.drift_log import DriftLog
from experience_engine.ports.revalidation_log import RevalidationLog


class RevalidationReplayMismatch(RuntimeError):
    """What is persisted is not what the persisted evidence and policy version recompute."""


@dataclass(frozen=True)
class RevalidationRun:
    overlay: DriftOverlay | None  # None unless the policy enforces the monitor
    new_transitions: list[RevalTransition] = field(default_factory=list)
    new_trials: list[RevalTrial] = field(default_factory=list)
    tokens: int = 0
    fleet: RevalidationFleet | None = None
    n_observations: int = 0


def _by_cell(ts: Sequence[RevalTransition]) -> dict[str, list[RevalTransition]]:
    out: dict[str, list[RevalTransition]] = defaultdict(list)
    for t in ts:
        out[t.cell_key].append(t)
    return {k: sorted(v, key=lambda x: x.seq) for k, v in out.items()}


def verify_transitions(
    persisted: Sequence[RevalTransition], recomputed: Sequence[RevalTransition]
) -> list[RevalTransition]:
    """The recomputed transitions not yet persisted; raises unless the log is exactly a prefix."""
    have, want = _by_cell(persisted), _by_cell(recomputed)
    new: list[RevalTransition] = []
    for cell, stored in have.items():
        mine = want.get(cell, [])
        if len(stored) > len(mine):
            raise RevalidationReplayMismatch(f"{cell}: {len(stored)} persisted > {len(mine)}")
        for a, b in zip(stored, mine, strict=False):
            if a.model_dump(mode="json") != b.model_dump(mode="json"):
                raise RevalidationReplayMismatch(
                    f"{cell}: persisted seq {a.seq} {a.signature()} != recomputed {b.signature()}"
                )
    for cell, mine in want.items():
        new += mine[len(have.get(cell, [])) :]
    return sorted(new, key=lambda t: (t.observed_at is None, t.observed_at, t.cell_key, t.seq))


def verify_trials(
    persisted: Sequence[RevalTrial], recomputed: Sequence[RevalTrial]
) -> list[RevalTrial]:
    """The recomputed trials not yet persisted. Every persisted trial must have been consumed by
    the recomputation with the same lineage verdict (otherwise it is evidence nothing explains)."""
    mine = {t.key.ident(): t for t in recomputed}
    for p in persisted:
        r = mine.get(p.key.ident())
        if r is None:
            raise RevalidationReplayMismatch(f"persisted trial {p.key.ident()} was never consumed")
        if (r.accepted, r.reject_reason, r.result) != (p.accepted, p.reject_reason, p.result):
            raise RevalidationReplayMismatch(f"trial {p.key.ident()} recomputes differently")
    stored = {p.key.ident() for p in persisted}
    return [t for t in recomputed if t.key.ident() not in stored]


class RevalidationService:
    def __init__(
        self,
        outcomes: OutcomeReader,
        log: RevalidationLog,
        drift_log: DriftLog,
        now: Callable[[], datetime],
        fallback: DriftService | None = None,
    ):
        self._outcomes = outcomes
        self._log = log
        self._drift_log = drift_log
        self._now = now
        self._fallback = fallback  # policies without revalidation keep their M6.2 service

    def _replay(
        self, definition: ControllerPolicyDefinition
    ) -> tuple[RevalidationFleet, list[DecisionOutcomeRecord], list[RevalTransition]]:
        dp, rp = definition.params.drift, definition.params.revalidation
        if dp is None or rp is None:
            raise ValueError("the policy has no drift monitor or no revalidation configuration")
        t, p, v = definition.tenant_id, definition.policy_id, definition.version
        records = self._outcomes(definition.tenant_id, [definition.policy_version])
        stored_t, stored_trials = self._log.transitions(t, p, v), self._log.trials(t, p, v)
        fleet = replay_revalidation(
            references_from_snapshot(definition.snapshot),
            dp,
            rp,
            definition.tenant_id,
            records,
            stored_t,
            stored_trials,
        )
        new_t = verify_transitions(stored_t, fleet.transitions)
        verify_trials(stored_trials, fleet.trials)
        return fleet, records, new_t

    def replay(self, definition: ControllerPolicyDefinition) -> RevalidationRun:
        """Recompute and verify without persisting (audits and dry runs)."""
        fleet, records, new_t = self._replay(definition)
        dp = definition.params.drift
        assert dp is not None
        overlay = fleet.overlay() if dp.mode == DriftMode.ENFORCE else None
        return RevalidationRun(overlay, new_t, [], 0, fleet, sum(1 for r in records if r.acted))

    def _persist(
        self,
        definition: ControllerPolicyDefinition,
        fleet: RevalidationFleet,
        new_t: Sequence[RevalTransition],
        new_trials: Sequence[RevalTrial],
    ) -> None:
        t, p, v = definition.tenant_id, definition.policy_id, definition.version
        rp, dp = definition.params.revalidation, definition.params.drift
        assert rp is not None and dp is not None
        for tr in new_t:  # defence in depth: the database checks this too
            if tr.to_state == RevalState.RECOVERED:
                problems = admissibility_problems(rp.recovery, rp.config_hash(), tr.gates)
                if problems:
                    raise RecoveryGateViolation(f"{tr.cell_key}: {'; '.join(problems)}")
        now = self._now()
        drift_new = verify_prefix(self._drift_log.transitions(t, p, v), fleet.monitor.transitions)
        self._log.append(t, p, v, new_t, new_trials, now, rp.model_dump(mode="json"))
        if drift_new:
            self._drift_log.append(t, p, v, drift_new, now, dp.model_dump(mode="json"))

    def sync(self, definition: ControllerPolicyDefinition) -> SyncResult:
        """Execution-plane entry (controller build): persists what the observations imply and
        returns the overlay. Never runs a trial."""
        if definition.params.revalidation is None:
            if self._fallback is None:
                return SyncResult(None)
            return self._fallback.sync(definition)
        fleet, records, new_t = self._replay(definition)
        self._persist(definition, fleet, new_t, [])
        dp = definition.params.drift
        assert dp is not None
        overlay = fleet.overlay() if dp.mode == DriftMode.ENFORCE else None
        return SyncResult(overlay, [], fleet.monitor, sum(1 for r in records if r.acted))

    def run(
        self, definition: ControllerPolicyDefinition, executor: TrialSource | None
    ) -> RevalidationRun:
        """Learning-plane worker: one live round on top of the verified replay."""
        fleet, records, new_t = self._replay(definition)
        stored = {x.key.ident() for x in fleet.trials}
        anchor = records[-1].decision_id if records else ""
        round_tr = fleet.step(executor, anchor, self._now())
        new_trials = [x for x in fleet.trials if x.key.ident() not in stored]
        persisted_t = [*new_t, *round_tr]
        self._persist(definition, fleet, persisted_t, new_trials)
        dp = definition.params.drift
        assert dp is not None
        overlay = fleet.overlay() if dp.mode == DriftMode.ENFORCE else None
        return RevalidationRun(
            overlay,
            persisted_t,
            new_trials,
            sum(x.result.tokens for x in new_trials),
            fleet,
            sum(1 for r in records if r.acted),
        )
