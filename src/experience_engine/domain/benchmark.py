import hashlib
import json
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from experience_engine.domain.decision import ControllerDecision
from experience_engine.domain.outcome import RunOutcome, TerminationReason
from experience_engine.domain.time import UtcDatetime
from experience_engine.domain.trace import StepRecord


class Split(StrEnum):
    DISCOVERY = "DISCOVERY"
    VALIDATION = "VALIDATION"
    HIDDEN_TRANSFER = "HIDDEN_TRANSFER"


class DatasetVersion(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    version: str
    split: Split
    content_hash: str
    n_tasks: int


class ModelSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    model: str
    temperature: float = 0.0
    seed: int | None = None
    max_output_tokens: int = 1024


class AgentConfig(BaseModel):
    """Fixed agent configuration. A_BASELINE: no persistent learning, memory or capabilities."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    config_id: str = "A_BASELINE"
    prompt_version: str
    model: ModelSettings


class BenchmarkRunRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str
    run_id: str
    correlation_id: str
    dataset: DatasetVersion
    seed: int
    config: AgentConfig
    runtime_version: str
    verifier_version: str
    environment_version: str
    tags: dict[str, str] = Field(default_factory=dict)
    decision: ControllerDecision
    outcome: RunOutcome
    termination: TerminationReason
    agent_claimed_done: bool
    steps: list[StepRecord]
    fresh_session: bool = True
    started_at: UtcDatetime
    ended_at: UtcDatetime

    def digest(self) -> str:
        """Hash of every deterministic field.

        Excludes IDs, timestamps and latency, which legitimately vary between reruns.
        """
        outcome = self.outcome.model_dump(
            mode="json", exclude={"run_id", "execution_time_ms", "evidence_refs"}
        )
        decision = self.decision.model_dump(mode="json", exclude={"decision_id", "run_id"})
        payload = {
            "dataset": self.dataset.model_dump(mode="json"),
            "seed": self.seed,
            "config": self.config.model_dump(mode="json"),
            "versions": [self.runtime_version, self.verifier_version, self.environment_version],
            "decision": decision,
            "outcome": outcome,
            "termination": self.termination.value,
            "claimed": self.agent_claimed_done,
            "steps": [s.model_dump(mode="json") for s in self.steps],
        }
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()
