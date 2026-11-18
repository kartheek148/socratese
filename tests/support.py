import json
from collections.abc import Iterator
from datetime import UTC, datetime

from experience_engine.bench.dataset import DEFAULT_SEED, BenchTask, generate_split
from experience_engine.bench.environment import SimulatedEnvironment
from experience_engine.domain.benchmark import AgentConfig, ModelSettings, Split
from experience_engine.execution.controller import FixedPolicy
from experience_engine.execution.orchestrator import TaskOrchestrator
from experience_engine.ports.llm import LLMError, LLMRequest, LLMResponse


class FakeClock:
    def __init__(self) -> None:
        self.ms = 0

    def now(self) -> datetime:
        return datetime(2025, 1, 1, tzinfo=UTC)

    def monotonic_ms(self) -> int:
        return self.ms


class ScriptedLLM:
    """Replays fixed replies (dict -> JSON text, str -> raw, Exception -> raised)."""

    def __init__(self, replies: list[object], clock: FakeClock | None = None, tick_ms: int = 0):
        self._replies: Iterator[object] = iter(replies)
        self._clock = clock
        self._tick = tick_ms
        self.calls = 0

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        if self._clock:
            self._clock.ms += self._tick
        reply = next(self._replies)
        if isinstance(reply, Exception):
            raise reply
        text = reply if isinstance(reply, str) else json.dumps(reply)
        return LLMResponse(text=text, prompt_tokens=10, completion_tokens=5)


def config() -> AgentConfig:
    return AgentConfig(prompt_version="test", model=ModelSettings(provider="fake", model="m"))


def first_task(family: str = "pool", variant: str = "sqlalchemy") -> BenchTask:
    return next(
        t
        for t in generate_split(Split.DISCOVERY, DEFAULT_SEED)
        if t.family == family and t.variant == variant
    )


def orchestrator(clock: FakeClock, verifier: object) -> TaskOrchestrator:
    return TaskOrchestrator(
        controller=FixedPolicy(),
        verifier=verifier,  # type: ignore[arg-type]
        clock=clock,
        config=config(),
    )


def session(task: BenchTask) -> SimulatedEnvironment:
    return SimulatedEnvironment(task.environment, task.task.allowed_tools)


def correct_pool_fix(task: BenchTask) -> list[object]:
    """Scripted correct solution for the sqlalchemy pool task."""
    doc = json.loads(task.environment.files["app/config.json"])
    demand = doc["server"]["workers"] * doc["server"]["threads_per_worker"]
    pool = max(1, demand - doc["database"]["max_overflow"])
    return [
        {
            "tool": "set_json_value",
            "args": {"path": "app/config.json", "key": "database.pool_size", "value": pool},
        },
        {"final": "fixed"},
    ]


__all__ = [
    "FakeClock",
    "LLMError",
    "ScriptedLLM",
    "config",
    "first_task",
    "orchestrator",
    "session",
]
