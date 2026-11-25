"""Memory registry service: the generic governance logic bound to memory definitions."""

from collections.abc import Mapping

from experience_engine.domain.capability import RiskLevelAtMost
from experience_engine.domain.evaluation import PromotionPolicy
from experience_engine.domain.memory import MemoryDefinition
from experience_engine.governance.registry import GovernedRegistry
from experience_engine.ports.artifacts import ArtifactStore
from experience_engine.ports.clock import Clock
from experience_engine.ports.registry import RegistryStore, VersionRecord


class MemoryRegistryService(GovernedRegistry[MemoryDefinition]):
    def __init__(
        self,
        store: RegistryStore[MemoryDefinition],
        policies: Mapping[str, PromotionPolicy],
        clock: Clock,
        artifacts: ArtifactStore | None = None,
    ):
        super().__init__(store, policies, clock, artifacts, MemoryRegistryService.risk_class_of)

    @staticmethod
    def risk_class_of(rec: VersionRecord[MemoryDefinition]) -> str:
        """Same rule as capabilities: the declared risk ceiling, else the strictest class."""
        for p in rec.definition.applicability_conditions:
            if isinstance(p, RiskLevelAtMost):
                return p.level
        return "high"
