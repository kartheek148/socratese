"""Persistence port for revalidation transitions and trial evidence (append-only audit, M6.3)."""

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from experience_engine.domain.revalidation import RevalTransition, RevalTrial


class RevalidationLog(Protocol):
    """Tenant-scoped. Rows are never updated or deleted; state is derived from them. `append`
    writes a round's trials and transitions atomically."""

    def transitions(
        self, tenant_id: str, policy_id: str, version: int
    ) -> list[RevalTransition]: ...

    def trials(self, tenant_id: str, policy_id: str, version: int) -> list[RevalTrial]: ...

    def append(
        self,
        tenant_id: str,
        policy_id: str,
        version: int,
        transitions: Sequence[RevalTransition],
        trials: Sequence[RevalTrial],
        now: datetime,
        config: dict[str, object],
    ) -> None: ...
