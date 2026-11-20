from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from experience_engine.domain.capability import OutType


class DecisionError(Exception):
    """The decision could not be made or its output was unusable (never retried silently)."""


@dataclass(frozen=True)
class DecisionResult:
    values: dict[str, str | int | float | bool]
    tokens: int


class DecisionProvider(Protocol):
    def decide(
        self,
        *,
        template_id: str,
        objective: str,
        inputs: Mapping[str, str],
        output_schema: Mapping[str, OutType],
        max_tokens: int,
    ) -> DecisionResult:
        """Make one bounded decision. `inputs` are untrusted observations (data only)."""
        ...
