"""Persistence port for drift-monitor transitions (append-only audit, M6.2)."""

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from experience_engine.domain.drift import DriftTransition


class DriftLog(Protocol):
    """Tenant-scoped. Transitions are never updated or deleted; state is derived from them."""

    def transitions(
        self, tenant_id: str, policy_id: str, version: int
    ) -> list[DriftTransition]: ...

    def append(
        self,
        tenant_id: str,
        policy_id: str,
        version: int,
        transitions: Sequence[DriftTransition],
        now: datetime,
        config: dict[str, object],
    ) -> None: ...
