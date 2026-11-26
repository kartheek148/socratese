"""Typed records for the capability-vs-memory experiment (pure data, JSON-serialisable)."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from experience_engine.domain.strategy import StrategyExecutionResult

ARMS = ("A", "B", "C", "D", "E", "E1", "F", "UB", "UC", "UD")
LEGACY_ARMS = ("A", "B", "C", "D", "E")
GATED_ARMS = ("A", "B", "C", "D", "E", "F")
SERVING_ARMS = ("A", "B", "C", "D", "E", "F")  # E1 is derived from E, never executed
ARM_NAMES = {
    "A": "stateless baseline",
    "B": "episodic memory",
    "C": "consolidated memory",
    "D": "instruction reuse",
    "E": "compiled capability",
    "E1": "capability phase only (derived from E)",
    "F": "noise control (empty registry)",
    "UB": "ungated episodic memory",
    "UC": "ungated consolidated memory",
    "UD": "ungated instruction reuse",
}
# gated mode: B-E read only ACTIVE assets and fall back to general planning with a reason
ARM_NAMES_GATED = {
    **ARM_NAMES,
    "B": "ACTIVE episodic memory only",
    "C": "ACTIVE consolidated memory only",
    "D": "ACTIVE procedural memory only",
    "E": "ACTIVE compiled capability only",
}
TransferLevel = Literal["source", "intra_variant", "cross_variant", "cross_family"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RunRow(_Frozen):
    """One execution of one arm on one task repetition, scored by the independent verifier."""

    arm: str
    task_id: str
    rep: int  # repetition index: repeated draws from a non-deterministic model, not a task sample
    split: str
    family: str
    variant: str
    level: TransferLevel
    status: str  # OutcomeStatus value; UNKNOWN is treated as missing, never as a failure
    verified: bool
    tokens: int  # all model tokens of the run, including retrieved memory inside the prompt
    tool_calls: int
    latency_ms: int
    retrieved_tokens: int = 0
    context_tokens: int = 0
    retrieval_ms: int = 0
    # arm E only
    applicable: bool | None = None  # an eligible capability existed for this task
    capability_used: bool | None = None
    fell_back: bool | None = None
    termination: str | None = None
    memory_sha256: str | None = None  # hash of the exact retrieved text (reproducibility)
    # governed assets (gated mode): the exact version used and why none was
    asset_kind: str | None = None  # CAPABILITY | MEMORY
    asset_ref: str | None = None  # id@version
    asset_family: str | None = None  # the family the asset declares
    fallback_reason: str | None = None
    # M5.10: capability / fallback phase attribution (serving mode only; absent in older results)
    strategy: StrategyExecutionResult | None = None


class LearningLedger(_Frozen):
    """Cost of producing the reusable experience, in tokens (plus wall time and tool calls)."""

    discovery_tokens: int
    discovery_tool_calls: int = 0
    discovery_runs: int = 0
    analysis_tokens: int = 0
    consolidation_tokens: int = 0
    compilation_tokens: int = 0
    validation_tokens: int = 0
    wall_ms_analysis: int = 0
    wall_ms_consolidation: int = 0
    wall_ms_compilation: int = 0
    wall_ms_memory_build: int = 0
    # gated mode: validation tokens actually recorded for each arm's asset type (all attempts,
    # rejected ones included); empty = use `validation_tokens` (legacy accounting)
    validation_by_arm: dict[str, int] = Field(default_factory=dict)
    validation_runs_by_arm: dict[str, int] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


class ExperimentConfig(_Frozen):
    """Everything needed to reproduce the run (no secrets, no private task contents)."""

    name: str
    model: dict[str, object]
    arms: list[str]
    repetitions: int
    workers: int
    datasets: dict[str, dict[str, str]]  # split -> name/version/content_hash
    discovery_trajectories: int
    discovery_families: list[str]
    capabilities: list[str]  # id@version of every capability available to arms D and E
    capability_catalog_states: list[str]
    memory_preamble_sha256: str
    max_episodes: int
    verifier_version: str
    runtime_version: str
    evaluator_version: str
    token_estimator: str = "chars/4"
    mode: str = "legacy"  # legacy | gated
    memory_assets: list[str] = Field(default_factory=list)  # id@version of ACTIVE memory used
    asset_states: list[str] = Field(
        default_factory=list
    )  # states the catalogs were allowed to read
    ungated_families: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ExperimentResult(_Frozen):
    config: ExperimentConfig
    ledger: LearningLedger
    ledger_by_family: dict[str, LearningLedger] = Field(default_factory=dict)
    rows: list[RunRow]
    applicability: list[dict[str, object]] = Field(default_factory=list)
