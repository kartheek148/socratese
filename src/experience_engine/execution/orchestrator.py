from dataclasses import dataclass

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
from experience_engine.domain.task import TaskSpec
from experience_engine.domain.time import UtcDatetime
from experience_engine.execution.controller import Controller, ControllerInput
from experience_engine.execution.runtime import AgentRuntime, RuntimeResult
from experience_engine.ports.clock import Clock
from experience_engine.ports.llm import LLMClient
from experience_engine.ports.tools import EnvironmentSession
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
    ):
        self._controller = controller
        self._verifier = verifier
        self._clock = clock
        self._config = config

    def execute(self, task: TaskSpec, session: EnvironmentSession, llm: LLMClient) -> RunResult:
        run_id = new_id()
        started_at = self._clock.now()
        t0 = self._clock.monotonic_ms()

        decision = self._controller.decide(
            ControllerInput(task=task, run_id=run_id, decision_id=new_id())
        )
        if decision.selected_action != Strategy.GENERAL_PLANNING:
            raise NotImplementedError(f"strategy {decision.selected_action} not available in M1")

        runtime = AgentRuntime(llm, session.tools, self._clock, self._config).run(task)

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
        return RunResult(
            run_id=run_id,
            started_at=started_at,
            ended_at=self._clock.now(),
            decision=decision.model_copy(update={"outcome_status": status}),
            runtime=runtime,
            outcome=outcome,
        )
