import hashlib
import json
from dataclasses import dataclass, field

from experience_engine.domain.benchmark import AgentConfig
from experience_engine.domain.outcome import TerminationReason
from experience_engine.domain.task import TaskSpec
from experience_engine.domain.trace import StepRecord
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
from experience_engine.ports.clock import Clock
from experience_engine.ports.llm import ChatMessage, LLMClient, LLMError, LLMRequest
from experience_engine.ports.tools import ToolGateway, ToolNotAllowedError

MAX_CONSECUTIVE_INVALID = 2


@dataclass
class RuntimeResult:
    termination: TerminationReason
    steps: list[StepRecord] = field(default_factory=list)
    tokens_used: int = 0
    tool_calls: int = 0
    human_interventions: int = 0
    agent_claimed_done: bool = False
    failure_reasons: list[str] = field(default_factory=list)


class AgentRuntime:
    """Bounded general-planning loop. Enforces step, tool-call, token and deadline budgets."""

    def __init__(self, llm: LLMClient, tools: ToolGateway, clock: Clock, config: AgentConfig):
        self._llm = llm
        self._tools = tools
        self._clock = clock
        self._config = config

    def run(self, task: TaskSpec) -> RuntimeResult:
        result = RuntimeResult(termination=TerminationReason.FINAL)
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
                return stop(TerminationReason.LLM_ERROR, f"llm error: {str(exc)[:200]}")
            llm_steps += 1
            used = response.prompt_tokens + response.completion_tokens
            result.tokens_used += used
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
                if invalid_streak >= MAX_CONSECUTIVE_INVALID:
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
                return stop(TerminationReason.TOOL_NOT_ALLOWED, str(exc)[:200])
            result.tool_calls += 1
            payload = tool_result.output.encode()
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
