from sqlalchemy import Engine

from experience_engine.adapters.db import tables as t
from experience_engine.adapters.db.asset_spec import AssetSpec
from experience_engine.adapters.db.registry_store import SqlAssetRegistryStore
from experience_engine.domain.memory import MemoryDefinition


def memory_spec() -> AssetSpec[MemoryDefinition]:
    return AssetSpec(
        identity=t.memories,
        versions=t.memory_versions,
        pointers=t.memory_pointers,
        transitions=t.memory_transitions,
        evaluations=t.memory_evaluations,
        observations=t.memory_observations,
        id_col="memory_id",
        verified_col="asset_verified",
        tokens_col="asset_tokens",
        decode=lambda raw: MemoryDefinition.model_validate(raw),
        audit_action="memory.transition",
        entity_type="memory_version",
    )


class SqlMemoryRegistryStore(SqlAssetRegistryStore[MemoryDefinition]):
    """Memory registry persistence: the same code as capabilities, over the memory tables."""

    def __init__(self, engine: Engine):
        super().__init__(engine, memory_spec())
