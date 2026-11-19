from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from experience_engine.domain.decision import ControllerDecision
from experience_engine.domain.outcome import RunOutcome
from experience_engine.domain.run import OutboxEvent, RunRecord
from experience_engine.domain.task import TaskSpec
from experience_engine.domain.trace import ArtifactRef, TraceEvent


@dataclass(frozen=True)
class RunReconstruction:
    run: RunRecord
    task: TaskSpec
    events: list[TraceEvent]
    decisions: list[ControllerDecision]
    outcome: RunOutcome | None


class RunLifecycle(Protocol):
    """Write side of run persistence. Every method is tenant-scoped via the argument objects."""

    def start_run(self, task: TaskSpec, run: RunRecord) -> None:
        """Upsert the task, insert the RUNNING run and an audit row (one transaction)."""
        ...

    def register_artifact(self, tenant_id: str, ref: ArtifactRef, uri: str) -> None: ...

    def append_event(self, event: TraceEvent) -> None: ...

    def record_decision(self, decision: ControllerDecision) -> None: ...

    def complete_run(
        self,
        outcome: RunOutcome,
        decision: ControllerDecision,
        ended_at: datetime,
        outbox_event: OutboxEvent,
    ) -> None:
        """One transaction: outcome, decision outcome back-fill, run status, audit, outbox."""
        ...


class RunReader(Protocol):
    def reconstruct(self, tenant_id: str, run_id: str) -> RunReconstruction | None: ...
