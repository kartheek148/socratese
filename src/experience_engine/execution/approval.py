"""Strategy approval: which ACTIVE assets a production strategy may use (M5.10, opt-in).

Three distinct decisions (docs/experiments/production-aligned-evaluation-design.md section 3.3):
(1) activation: the asset is validated and ACTIVE (registry); (2) strategy approval: the serving
strategy using that asset is approved (these wrappers); (3) per-task routing: the deterministic
matcher / rule controller decides whether it applies to one task. The wrappers only ever REMOVE
assets from what the inner catalog offers: they can never widen an ACTIVE-only view.
"""

from collections.abc import Collection, Sequence

from experience_engine.domain.capability import CapabilityDefinition
from experience_engine.domain.memory import MemoryDefinition
from experience_engine.ports.experience import ExecutableCapabilityCatalog
from experience_engine.ports.memory import ActiveMemoryCatalog


class ApprovedCapabilityCatalog:
    def __init__(self, inner: ExecutableCapabilityCatalog, approved_refs: Collection[str]):
        self._inner = inner
        self._approved = frozenset(approved_refs)

    def list_executable(self, tenant_id: str) -> list[CapabilityDefinition]:
        return [
            d
            for d in self._inner.list_executable(tenant_id)
            if f"{d.id}@{d.version}" in self._approved
        ]

    def get_executable(
        self, tenant_id: str, capability_id: str, version: int
    ) -> CapabilityDefinition | None:
        if f"{capability_id}@{version}" not in self._approved:
            return None
        return self._inner.get_executable(tenant_id, capability_id, version)


class ApprovedMemoryCatalog:
    def __init__(self, inner: ActiveMemoryCatalog, approved_refs: Collection[str]):
        self._inner = inner
        self._approved = frozenset(approved_refs)

    def list_active(self, tenant_id: str) -> Sequence[MemoryDefinition]:
        return [
            d
            for d in self._inner.list_active(tenant_id)
            if f"{d.memory_id}@{d.version}" in self._approved
        ]
