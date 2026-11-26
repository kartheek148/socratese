import json
from collections.abc import Mapping
from dataclasses import dataclass

from experience_engine import RUNTIME_VERSION
from experience_engine.capabilities.applicability import EnvironmentFacts
from experience_engine.capabilities.executor import (
    CapabilityExecutor,
    ExecStatus,
    ExecutionResult,
)
from experience_engine.capabilities.tools import classify_path
from experience_engine.domain.benchmark import AgentConfig
from experience_engine.domain.capability import CapabilityDefinition
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
from experience_engine.domain.strategy import CapabilityPhase, FallbackPhase, fallback_cause
from experience_engine.domain.task import TaskSpec
from experience_engine.domain.time import UtcDatetime
from experience_engine.domain.trace import StepRecord, TraceEventType
from experience_engine.execution.capability_runtime import CapabilityRuntime, TracingToolGateway
from experience_engine.execution.controller import (
    SELECTED_CAPABILITY_KEY,
    Controller,
    ControllerInput,
)
from experience_engine.execution.matcher import MatchResult
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
        capabilities: CapabilityRuntime | None = None,
        attribute_phases: bool = False,
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
        self._capabilities = capabilities
        # Opt-in: verify the environment at the capability -> planner handover so the capability's
        # own outcome is recorded independently (one extra read-only verifier call). Default off:
        # production behaviour, budgets and traces are unchanged.
        self._attribute = attribute_phases

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

        tags = dict(run_tags or {})
        facts: EnvironmentFacts | None = None
        match: MatchResult | None = None
        if self._capabilities is not None:
            roles = {classify_path(p) for p in session.snapshot()}
            facts = EnvironmentFacts.of(
                task.task_family, task.allowed_tools, task.risk_level, roles
            )
            match = self._capabilities.matcher.match(task, facts)
            if self._capabilities.experimental:
                tags["capability_catalog"] = "experimental"
        decision = self._controller.decide(
            ControllerInput(
                task=task,
                run_id=run_id,
                decision_id=new_id(),
                eligible_capabilities=[e.ref for e in match.eligible] if match else [],
                match=match,
            )
        )

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
                    tags=tags,
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

        if match is not None:
            sink.emit(
                EmitSpec(
                    type=TraceEventType.CAPABILITY_MATCH,
                    component="capability_matcher",
                    component_version=RUNTIME_VERSION,
                    action="match",
                    status="eligible" if match.eligible else "none_eligible",
                    metrics={
                        "n_catalog": match.n_catalog,
                        "n_eligible": len(match.eligible),
                        "n_rejected": len(match.rejected),
                    },
                    payloads={
                        "candidates": Payload(json.dumps(match.snapshot(50)), "application/json")
                    },
                    parent_event_id=decision_event,
                )
            )
        runtime, capability_ref = self._execute_strategy(
            task=task,
            session=session,
            llm=llm,
            decision=decision,
            match=match,
            facts=facts,
            sink=sink,
            decision_event=decision_event,
            t0=t0,
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
            capability_version=capability_ref,
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

    # ---- strategy dispatch --------------------------------------------------------------------
    def _general(
        self,
        task: TaskSpec,
        session: EnvironmentSession,
        llm: LLMClient,
        sink: TraceSink,
        parent_event: str | None,
    ) -> RuntimeResult:
        return AgentRuntime(llm, session.tools, self._clock, self._config, sink).run(
            task, parent_event
        )

    def _execute_strategy(
        self,
        *,
        task: TaskSpec,
        session: EnvironmentSession,
        llm: LLMClient,
        decision: ControllerDecision,
        match: MatchResult | None,
        facts: EnvironmentFacts | None,
        sink: TraceSink,
        decision_event: str,
        t0: int,
    ) -> tuple[RuntimeResult, str | None]:
        """Run the selected strategy. Returns the result and the capability ref used (if any)."""
        action = decision.selected_action
        if action == Strategy.GENERAL_PLANNING:
            return self._general(task, session, llm, sink, decision_event), None
        if action == Strategy.HUMAN_ESCALATION:
            return RuntimeResult(TerminationReason.ESCALATED, human_interventions=1), None

        ref = decision.feature_snapshot.get(SELECTED_CAPABILITY_KEY)
        chosen = (
            next((e for e in match.eligible if e.ref == ref), None)
            if match is not None and isinstance(ref, str)
            else None
        )
        if chosen is None or self._capabilities is None or facts is None:
            # A controller must never select a capability the eligibility gate did not pass.
            raise ValueError(f"controller selected an ineligible capability: {ref!r}")

        caps = self._capabilities
        gateway = TracingToolGateway(session.tools, sink, decision_event)

        def verify() -> bool | None:
            status = aggregate_status(self._verifier.verify(task, dict(session.snapshot())))
            return (
                None
                if status == OutcomeStatus.UNKNOWN
                else status == OutcomeStatus.VERIFIED_SUCCESS
            )

        def resolve(dep: str) -> CapabilityDefinition | None:
            dep_id, _, version = dep.partition("@")
            if not version.isdigit():
                return None
            return caps.catalog.get_executable(task.tenant_id, dep_id, int(version))

        executor = CapabilityExecutor(
            tools=gateway,
            decisions=caps.decisions_for(llm),
            clock=self._clock,
            verify=verify,
            resolver=resolve,
        )
        cap_started = self._clock.monotonic_ms()
        exec_result = executor.run(
            chosen.definition, capability_tenant=task.tenant_id, task=task, facts=facts
        )
        cap_latency = self._clock.monotonic_ms() - cap_started
        exec_event = sink.emit(
            EmitSpec(
                type=TraceEventType.CAPABILITY_EXEC,
                component="capability_executor",
                component_version=RUNTIME_VERSION,
                action=chosen.ref,
                status=exec_result.status.value,
                metrics={
                    "reason": exec_result.reason.value,
                    "steps_executed": exec_result.steps_executed,
                    "tokens": exec_result.tokens_used,
                    "tool_calls": exec_result.tool_calls,
                    "verified_by_capability": exec_result.verified_by_capability,
                },
                payloads={
                    "steps": Payload(
                        json.dumps(
                            [
                                {"op": s.op, "id": s.step_id, "ok": s.ok, "note": s.note[:120]}
                                for s in exec_result.steps
                            ]
                        ),
                        "application/json",
                    )
                },
                parent_event_id=gateway.last_event_id or decision_event,
            )
        )
        base = RuntimeResult(
            TerminationReason.CAPABILITY_COMPLETED,
            tokens_used=exec_result.tokens_used,
            tool_calls=exec_result.tool_calls,
            capability_phase=CapabilityPhase(
                status=exec_result.status.value,
                reason=exec_result.reason.value,
                detail=exec_result.detail[:200],
                tokens=exec_result.tokens_used,
                tool_calls=exec_result.tool_calls,
                steps=exec_result.steps_executed,
                latency_ms=cap_latency,
                outcome_measured=False,
            ),
        )
        why = f"{chosen.ref}: {exec_result.reason.value} {exec_result.detail}".strip()[:300]
        if exec_result.status == ExecStatus.COMPLETED:
            base.agent_claimed_done = True
            return base, chosen.ref
        if exec_result.status == ExecStatus.ESCALATE:
            base.termination = TerminationReason.ESCALATED
            base.human_interventions = 1
            base.failure_reasons.append(why)
            return base, chosen.ref
        if exec_result.status == ExecStatus.ABORTED:
            base.termination = TerminationReason.CAPABILITY_ABORTED
            base.failure_reasons.append(why)
            return base, chosen.ref
        return (
            self._fallback(
                task, session, llm, sink, exec_event, exec_result, base, t0, why, chosen.ref
            ),
            chosen.ref,
        )

    def _fallback(
        self,
        task: TaskSpec,
        session: EnvironmentSession,
        llm: LLMClient,
        sink: TraceSink,
        parent_event: str,
        exec_result: ExecutionResult,
        spent: RuntimeResult,
        t0: int,
        why: str,
        ref: str,
    ) -> RuntimeResult:
        """General planning with whatever budget the capability left. Budgets are never reset.

        The capability may already have changed the environment; the planner works on the
        current state, and the independent verifier judges the final state either way.
        """
        handover = self._handover_status(task, session)
        spent.handover_status = handover
        fb_started = self._clock.monotonic_ms()
        remaining = {
            "max_tokens": task.max_tokens - exec_result.tokens_used,
            "max_steps": task.max_steps - exec_result.steps_executed,
            "max_tool_calls": task.max_tool_calls - exec_result.tool_calls,
            "deadline_ms": task.deadline_ms - (self._clock.monotonic_ms() - t0),
        }
        event = sink.emit(
            EmitSpec(
                type=TraceEventType.FALLBACK,
                component="orchestrator",
                component_version=RUNTIME_VERSION,
                action="general_planning",
                status=exec_result.reason.value,
                metrics={"capability": ref, **{f"remaining_{k}": v for k, v in remaining.items()}},
                parent_event_id=parent_event,
            )
        )
        exhausted = {
            "max_tokens": TerminationReason.MAX_TOKENS,
            "max_steps": TerminationReason.MAX_STEPS,
            "max_tool_calls": TerminationReason.MAX_TOOL_CALLS,
            "deadline_ms": TerminationReason.DEADLINE,
        }
        cause = fallback_cause(exec_result.reason.value)
        for key, reason in exhausted.items():
            if remaining[key] <= 0:
                spent.fallback_phase = FallbackPhase(
                    triggered=True,
                    attempted=False,
                    cause=cause,
                    reason=why,
                    remaining_tokens=remaining["max_tokens"],
                    remaining_tool_calls=remaining["max_tool_calls"],
                )
                spent.termination = reason
                spent.fallback_reason = why
                spent.failure_reasons += [why, f"no {key} left for fallback"]
                return spent
        fallback = self._general(task.model_copy(update=remaining), session, llm, sink, event)
        steps: list[StepRecord] = fallback.steps
        return RuntimeResult(
            termination=fallback.termination,
            steps=steps,
            tokens_used=spent.tokens_used + fallback.tokens_used,
            prompt_tokens=spent.prompt_tokens + fallback.prompt_tokens,
            completion_tokens=spent.completion_tokens + fallback.completion_tokens,
            tool_calls=spent.tool_calls + fallback.tool_calls,
            human_interventions=fallback.human_interventions,
            agent_claimed_done=fallback.agent_claimed_done,
            failure_reasons=[why, *fallback.failure_reasons],
            fallback_reason=why,
            capability_phase=spent.capability_phase,
            handover_status=handover,
            fallback_phase=FallbackPhase(
                triggered=True,
                attempted=True,
                cause=cause,
                reason=why,
                remaining_tokens=remaining["max_tokens"],
                remaining_tool_calls=remaining["max_tool_calls"],
                tokens=fallback.tokens_used,
                prompt_tokens=fallback.prompt_tokens,
                completion_tokens=fallback.completion_tokens,
                tool_calls=fallback.tool_calls,
                latency_ms=self._clock.monotonic_ms() - fb_started,
            ),
        )

    def _handover_status(self, task: TaskSpec, session: EnvironmentSession) -> str | None:
        """Independent verifier status of the state the capability left (opt-in, read-only)."""
        if not self._attribute:
            return None
        try:
            return aggregate_status(self._verifier.verify(task, dict(session.snapshot()))).value
        except Exception:  # a verifier error is UNKNOWN, never a guess
            return OutcomeStatus.UNKNOWN.value
