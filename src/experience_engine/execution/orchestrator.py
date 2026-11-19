import json
from collections.abc import Mapping
from dataclasses import dataclass

from experience_engine import RUNTIME_VERSION
from experience_engine.domain.benchmark import AgentConfig
from experience_engine.domain.decision import ControllerDecision, Strategy
from experience_engine.domain.ids import new_id
from experience_engine.domain.outcome import (
    OutcomeStatus,
    RunOutcome,
    TerminationReason,
    VerificationResult,
    aggregate_status,
    quality_score,
)
from experience_engine.domain.redaction import Redactor
from experience_engine.domain.run import (
    EXECUTION_COMPLETED,
    ExecutionCompletedPayload,
    OutboxEvent,
    RunRecord,
)
from experience_engine.domain.task import TaskSpec
from experience_engine.domain.time import UtcDatetime
from experience_engine.domain.trace import TraceEventType
from experience_engine.execution.controller import Controller, ControllerInput
from experience_engine.execution.recorder import NullTraceSink, RunRecorder
from experience_engine.execution.runtime import AgentRuntime, RuntimeResult
from experience_engine.ports.artifacts import ArtifactStore
from experience_engine.ports.clock import Clock
from experience_engine.ports.llm import LLMClient
from experience_engine.ports.persistence import RunLifecycle
from experience_engine.ports.tools import EnvironmentSession
from experience_engine.ports.tracing import EmitSpec, Payload, TraceSink
from experience_engine.ports.verifier import OutcomeVerifier


@dataclass(frozen=True)
class RunResult:
    run_id: str
    started_at: UtcDatetime
    ended_at: UtcDatetime
    decision: ControllerDecision
    runtime: RuntimeResult
    outcome: RunOutcome


class TaskOrchestrator:
    """Coordinates controller -> runtime -> independent verifier for one task run.

    The verifier sees only the environment snapshot, never the agent's final message.
    Execution never waits on learning (there is none in the baseline).
    """

    def __init__(
        self,
        *,
        controller: Controller,
        verifier: OutcomeVerifier,
        clock: Clock,
        config: AgentConfig,
        lifecycle: RunLifecycle | None = None,
        artifacts: ArtifactStore | None = None,
        redactor: Redactor | None = None,
    ):
        if (lifecycle is None) != (artifacts is None):
            raise ValueError("lifecycle and artifacts must be configured together")
        self._controller = controller
        self._verifier = verifier
        self._clock = clock
        self._config = config
        self._lifecycle = lifecycle
        self._artifacts = artifacts
        self._redactor = redactor or Redactor()

    def execute(
        self,
        task: TaskSpec,
        session: EnvironmentSession,
        llm: LLMClient,
        run_tags: Mapping[str, str] | None = None,
    ) -> RunResult:
        run_id = new_id()
        started_at = self._clock.now()
        t0 = self._clock.monotonic_ms()

        decision = self._controller.decide(
            ControllerInput(task=task, run_id=run_id, decision_id=new_id())
        )
        if decision.selected_action != Strategy.GENERAL_PLANNING:
            raise NotImplementedError(f"strategy {decision.selected_action} not available in M1")

        sink: TraceSink = NullTraceSink()
        if self._lifecycle is not None and self._artifacts is not None:
            self._lifecycle.start_run(
                task,
                RunRecord(
                    run_id=run_id,
                    tenant_id=task.tenant_id,
                    task_id=task.task_id,
                    correlation_id=run_id,
                    config_id=self._config.config_id,
                    policy_version=decision.policy_version,
                    runtime_version=RUNTIME_VERSION,
                    model_settings=self._config.model.model_dump(mode="json"),
                    tags=dict(run_tags or {}),
                    started_at=started_at,
                ),
            )
            self._lifecycle.record_decision(decision)
            sink = RunRecorder(
                lifecycle=self._lifecycle,
                artifacts=self._artifacts,
                redactor=self._redactor,
                clock=self._clock,
                tenant_id=task.tenant_id,
                task_id=task.task_id,
                run_id=run_id,
                trace_id=run_id,
            )
        decision_event = sink.emit(
            EmitSpec(
                type=TraceEventType.DECISION,
                component="controller",
                component_version=decision.policy_version,
                action=decision.selected_action.value,
                status="selected",
                metrics={
                    "decision_id": decision.decision_id,
                    "n_eligible": len(decision.eligible_actions),
                    "selection_probability": decision.selection_probability,
                },
            )
        )

        runtime = AgentRuntime(llm, session.tools, self._clock, self._config, sink).run(
            task, decision_event
        )
        sink.emit(
            EmitSpec(
                type=TraceEventType.RUN_TERMINATED,
                component="runtime",
                component_version=RUNTIME_VERSION,
                action=runtime.termination.value,
                status="claimed_done" if runtime.agent_claimed_done else "stopped",
                metrics={
                    "tokens_used": runtime.tokens_used,
                    "tool_calls": runtime.tool_calls,
                    "human_interventions": runtime.human_interventions,
                },
                parent_event_id=decision_event,
            )
        )

        try:
            results = self._verifier.verify(task, dict(session.snapshot()))
        except Exception as exc:  # verifier failure must yield UNKNOWN, never a guess
            results = [
                VerificationResult(
                    criterion_kind="verifier_error",
                    role="goal",
                    passed=None,
                    detail=f"verifier raised {type(exc).__name__}"[:500],
                )
            ]
        status = aggregate_status(results)
        sink.emit(
            EmitSpec(
                type=TraceEventType.VERIFICATION,
                component="verifier",
                component_version=self._verifier.version,
                action="verify",
                status=status.value,
                metrics={
                    "n_criteria": len(results),
                    "n_passed": sum(1 for r in results if r.passed is True),
                    "n_failed": sum(1 for r in results if r.passed is False),
                    "n_unknown": sum(1 for r in results if r.passed is None),
                },
                payloads={
                    "results": Payload(
                        json.dumps([r.model_dump(mode="json") for r in results]),
                        "application/json",
                    )
                },
                parent_event_id=decision_event,
            )
        )
        failure_reasons = list(runtime.failure_reasons)
        if runtime.termination == TerminationReason.LLM_ERROR and (
            status != OutcomeStatus.VERIFIED_SUCCESS
        ):
            # Provider outage is not evidence about the agent.
            status = OutcomeStatus.UNKNOWN
        failure_reasons += [r.detail for r in results if r.passed is False]

        elapsed = self._clock.monotonic_ms() - t0
        outcome = RunOutcome(
            run_id=run_id,
            task_id=task.task_id,
            tenant_id=task.tenant_id,
            config_id=self._config.config_id,
            policy_version=decision.policy_version,
            status=status,
            quality_score=quality_score(results),
            verification_results=results,
            tokens_used=runtime.tokens_used,
            tool_calls=runtime.tool_calls,
            execution_time_ms=elapsed,
            human_interventions=runtime.human_interventions,
            failure_reasons=failure_reasons,
            evidence_refs=[r.evidence_ref for r in results if r.evidence_ref],
            verifier_version=self._verifier.version,
        )
        ended_at = self._clock.now()
        final_decision = decision.model_copy(update={"outcome_status": status})
        if self._lifecycle is not None:
            event_id = new_id()
            # Single transaction: outcome + decision back-fill + run status + audit + outbox row.
            self._lifecycle.complete_run(
                outcome,
                final_decision,
                ended_at,
                OutboxEvent(
                    event_id=event_id,
                    tenant_id=task.tenant_id,
                    type=EXECUTION_COMPLETED,
                    aggregate_id=run_id,
                    correlation_id=run_id,
                    created_at=ended_at,
                    payload=ExecutionCompletedPayload(
                        event_id=event_id,
                        tenant_id=task.tenant_id,
                        run_id=run_id,
                        task_id=task.task_id,
                        correlation_id=run_id,
                        outcome_status=status,
                        config_id=self._config.config_id,
                        policy_version=decision.policy_version,
                        verifier_version=self._verifier.version,
                        occurred_at=ended_at,
                    ).model_dump(mode="json"),
                ),
            )
        return RunResult(
            run_id=run_id,
            started_at=started_at,
            ended_at=ended_at,
            decision=final_decision,
            runtime=runtime,
            outcome=outcome,
        )
