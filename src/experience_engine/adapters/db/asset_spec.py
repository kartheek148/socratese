"""Table layout of one governed asset kind, so one SQL store serves capabilities and memory."""

from collections.abc import Callable
from dataclasses import dataclass

import sqlalchemy as sa

from experience_engine.adapters.db import tables as t


@dataclass(frozen=True)
class AssetSpec[D]:
    identity: sa.Table
    versions: sa.Table
    pointers: sa.Table
    transitions: sa.Table
    evaluations: sa.Table
    observations: sa.Table
    id_col: str
    verified_col: str  # observation column: did the asset arm verify
    tokens_col: str
    decode: Callable[[object], D]
    audit_action: str
    entity_type: str


def capability_spec[D](decode: Callable[[object], D]) -> AssetSpec[D]:
    return AssetSpec(
        identity=t.capabilities,
        versions=t.capability_versions,
        pointers=t.capability_pointers,
        transitions=t.capability_transitions,
        evaluations=t.capability_evaluations,
        observations=t.rollout_observations,
        id_col="capability_id",
        verified_col="capability_verified",
        tokens_col="capability_tokens",
        decode=decode,
        audit_action="capability.transition",
        entity_type="capability_version",
    )
