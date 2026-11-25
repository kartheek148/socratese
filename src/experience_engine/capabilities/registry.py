"""Capability registry service: the generic governance logic bound to capability definitions.

Behaviour is identical to the pre-M5.8 implementation; the logic itself now lives in
`governance.registry.GovernedRegistry` so memory artifacts are promoted by the same code.
"""

from collections.abc import Mapping

from experience_engine.domain.capability import CapabilityDefinition, RiskLevelAtMost
from experience_engine.domain.evaluation import PromotionPolicy
from experience_engine.governance.registry import (
    GovernedRegistry,
    IllegalTransitionError,
    PromotionBlockedError,
    RegistryError,
)
from experience_engine.ports.artifacts import ArtifactStore
from experience_engine.ports.clock import Clock
from experience_engine.ports.registry import RegistryStore, VersionRecord

__all__ = [
    "IllegalTransitionError",
    "PromotionBlockedError",
    "RegistryError",
    "RegistryService",
]


class RegistryService(GovernedRegistry[CapabilityDefinition]):
    def __init__(
        self,
        store: RegistryStore[CapabilityDefinition],
        policies: Mapping[str, PromotionPolicy],
        clock: Clock,
        artifacts: ArtifactStore | None = None,
    ):
        super().__init__(store, policies, clock, artifacts, RegistryService.risk_class_of)

    @staticmethod
    def risk_class_of(rec: VersionRecord[CapabilityDefinition]) -> str:
        """Strictest class unless the definition declares a narrower risk ceiling."""
        for p in rec.definition.preconditions:
            if isinstance(p, RiskLevelAtMost):
                return p.level
        return "high"
