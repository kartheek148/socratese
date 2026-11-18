from collections.abc import Mapping
from typing import Protocol

from experience_engine.domain.outcome import VerificationResult
from experience_engine.domain.task import TaskSpec


class OutcomeVerifier(Protocol):
    """Verifies from environment evidence only; the agent's transcript/claims are not an input."""

    version: str

    def verify(
        self, task: TaskSpec, snapshot: Mapping[str, str] | None
    ) -> list[VerificationResult]: ...
