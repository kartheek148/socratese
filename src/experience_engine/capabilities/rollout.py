"""Deterministic rollout routing for SHADOW / CANARY / ACTIVE versions (used by milestone 5)."""

import hashlib
from enum import StrEnum

from experience_engine.domain.capability import CapabilityState


class Route(StrEnum):
    SERVE = "SERVE"  # the capability handles the task
    SHADOW = "SHADOW"  # baseline serves; capability runs silently and is only observed
    SKIP = "SKIP"  # capability is not involved


def bucket(capability_id: str, task_id: str) -> float:
    """Stable value in [0, 1) so the same task always gets the same canary decision."""
    digest = hashlib.sha256(f"{capability_id}:{task_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def route(
    state: CapabilityState, canary_fraction: float, capability_id: str, task_id: str
) -> Route:
    if state == CapabilityState.ACTIVE:
        return Route.SERVE
    if state == CapabilityState.CANARY:
        return Route.SERVE if bucket(capability_id, task_id) < canary_fraction else Route.SKIP
    if state == CapabilityState.SHADOW:
        return Route.SHADOW
    return Route.SKIP
