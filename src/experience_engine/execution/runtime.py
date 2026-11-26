import hashlib
import json
from dataclasses import dataclass, field

from experience_engine import RUNTIME_VERSION
from experience_engine.domain.benchmark import AgentConfig
from experience_engine.domain.outcome import TerminationReason
from experience_engine.domain.task import TaskSpec
from experience_engine.domain.trace import StepRecord, TraceEventType
from experience_engine.execution.protocol import (
    EscalateAction,
    FinalAction,
    InvalidActionError,
    ToolAction,
    format_error,
    format_observation,
    parse_action,
    system_prompt,
)
from experience_engine.execution.recorder import NullTraceSink
from experience_engine.ports.clock import Clock
from experience_engine.ports.llm import ChatMessage, LLMClient, LLMError, LLMRequest
from experience_engine.ports.tools import ToolGateway, ToolNotAllowedError
from experience_engine.ports.tracing import EmitSpec, Payload, TraceSink

MAX_CONSECUTIVE_INVALID = 2
_JSON = "application/json"


@dataclass
class RuntimeResult:
    termination: TerminationReason
    steps: list[StepRecord] = field(default_factory=list)
    tokens_used: int = 0
    prompt_tokens: int = 0  # split known only for general-planning model calls
    completion_tokens: int = 0
    tool_calls: int = 0
    human_interventions: int = 0
    agent_claimed_done: bool = False
    failure_reasons: list[str] = field(default_factory=list)
    fallback_reason: str | None = None  # set when a capability handed over to general planning


class AgentRuntime:
    """Bounded general-planning loop. Enforces step, tool-call, token and deadline budgets."""

    def __init__(
        self,
        llm: LLMClient,
        tools: ToolGateway,
        clock: Clock,
        config: AgentConfig,
        trace: TraceSink | None = None,
    ):
        self._llm = llm
        self._tools = tools
        self._clock = clock
        self._config = config
        self._trace: TraceSink = trace or NullTraceSink()

    def _emit(self, spec: EmitSpec) -> str:
        return self._trace.emit(spec)

    @staticmethod
    def _request_payload(messages: list[ChatMessage]) -> Payload:
        return Payload(json.dumps([m.model_dump() for m in messages]), _JSON)

    def run(self, task: TaskSpec, parent_event_id: str | None = None) -> RuntimeResult:
        result = RuntimeResult(termination=TerminationReason.FINAL)
        last_event = parent_event_id

        messages = [
            ChatMessage(role="system", content=system_prompt(self._tools.describe())),
            ChatMessage(
                role="user",
                content=f"{task.objective}\nContext: {json.dumps(task.inputs, sort_keys=True)}",
            ),
        ]
        start = self._clock.monotonic_ms()
        llm_steps = 0
        invalid_streak = 0

        def stop(reason: TerminationReason, why: str) -> RuntimeResult:
            result.termination = reason
            result.failure_reasons.append(why)
            return result

        while True:
            if llm_steps >= task.max_steps:
                return stop(TerminationReason.MAX_STEPS, f"exceeded max_steps={task.max_steps}")
            if self._clock.monotonic_ms() - start >= task.deadline_ms:
                return stop(TerminationReason.DEADLINE, f"exceeded deadline_ms={task.deadline_ms}")
            try:
                response = self._llm.complete(
                    LLMRequest(messages=messages, model=self._config.model)
                )
            except LLMError as exc:
                self._emit(
                    EmitSpec(
                        type=TraceEventType.MODEL_CALL,
                        component="llm",
                        component_version=self._config.model.model,
                        action="complete",
                        status="error",
                        metrics={"step": llm_steps, "error": str(exc)[:200]},
                        payloads={"request": self._request_payload(messages)},
                        parent_event_id=last_event,
                    )
                )
                return stop(TerminationReason.LLM_ERROR, f"llm error: {str(exc)[:200]}")
            llm_steps += 1
            used = response.prompt_tokens + response.completion_tokens
            result.tokens_used += used
            result.prompt_tokens += response.prompt_tokens
            result.completion_tokens += response.completion_tokens
            model_event = self._emit(
                EmitSpec(
                    type=TraceEventType.MODEL_CALL,
                    component="llm",
                    component_version=self._config.model.model,
                    action="complete",
                    status="ok",
                    metrics={
                        "step": llm_steps,
                        "prompt_tokens": response.prompt_tokens,
                        "completion_tokens": response.completion_tokens,
                        "prompt_version": self._config.prompt_version,
                    },
                    payloads={
                        "request": self._request_payload(messages),
                        "response": Payload(response.text),
                    },
                    parent_event_id=last_event,
                )
            )
            last_event = model_event
            result.steps.append(
                StepRecord(
                    index=len(result.steps),
                    kind="llm",
                    tokens=used,
                    summary=response.text[:200].replace("\n", " "),
                    size_bytes=len(response.text.encode()),
                    sha256=hashlib.sha256(response.text.encode()).hexdigest(),
                )
            )
            if result.tokens_used > task.max_tokens:
                return stop(TerminationReason.MAX_TOKENS, f"exceeded max_tokens={task.max_tokens}")
            if self._clock.monotonic_ms() - start >= task.deadline_ms:
                return stop(TerminationReason.DEADLINE, f"exceeded deadline_ms={task.deadline_ms}")

            messages.append(ChatMessage(role="assistant", content=response.text))
            try:
                action = parse_action(response.text)
            except InvalidActionError as exc:
                invalid_streak += 1
                fatal = invalid_streak >= MAX_CONSECUTIVE_INVALID
                last_event = self._emit(
                    EmitSpec(
                        type=TraceEventType.INVALID_ACTION,
                        component="runtime",
                        component_version=RUNTIME_VERSION,
                        action="parse_action",
                        status="fatal" if fatal else "retry",
                        metrics={"error": str(exc)[:200], "streak": invalid_streak},
                        parent_event_id=last_event,
                    )
                )
                if fatal:
                    return stop(TerminationReason.INVALID_ACTION, f"invalid action: {exc}")
                messages.append(ChatMessage(role="user", content=format_error(str(exc))))
                continue
            invalid_streak = 0

            if isinstance(action, FinalAction):
                result.agent_claimed_done = True
                result.termination = TerminationReason.FINAL
                return result
            if isinstance(action, EscalateAction):
                result.human_interventions += 1
                result.termination = TerminationReason.ESCALATED
                return result

            assert isinstance(action, ToolAction)
            if result.tool_calls >= task.max_tool_calls:
                return stop(
                    TerminationReason.MAX_TOOL_CALLS,
                    f"exceeded max_tool_calls={task.max_tool_calls}",
                )
            try:
                tool_result = self._tools.call(action.tool, action.args)
            except ToolNotAllowedError as exc:
                self._emit(
                    EmitSpec(
                        type=TraceEventType.TOOL_CALL,
                        component="tool_gateway",
                        component_version=RUNTIME_VERSION,
                        action=action.tool[:100],
                        status="denied",
                        metrics={"reason": str(exc)[:200]},
                        payloads={"args": Payload(json.dumps(action.args, sort_keys=True), _JSON)},
                        parent_event_id=last_event,
                    )
                )
                return stop(TerminationReason.TOOL_NOT_ALLOWED, str(exc)[:200])
            result.tool_calls += 1
            payload = tool_result.output.encode()
            last_event = self._emit(
                EmitSpec(
                    type=TraceEventType.TOOL_CALL,
                    component="tool_gateway",
                    component_version=RUNTIME_VERSION,
                    action=action.tool[:100],
                    status="ok" if tool_result.ok else "failed",
                    metrics={"output_bytes": len(payload)},
                    payloads={
                        "args": Payload(json.dumps(action.args, sort_keys=True), _JSON),
                        "output": Payload(tool_result.output),
                    },
                    parent_event_id=last_event,
                )
            )
            result.steps.append(
                StepRecord(
                    index=len(result.steps),
                    kind="tool",
                    tool=action.tool,
                    ok=tool_result.ok,
                    summary=f"{action.tool} {json.dumps(action.args, sort_keys=True)}"[:200],
                    size_bytes=len(payload),
                    sha256=hashlib.sha256(payload).hexdigest(),
                )
            )
            messages.append(
                ChatMessage(
                    role="user",
                    content=format_observation(action.tool, action.args, tool_result),
                )
            )
