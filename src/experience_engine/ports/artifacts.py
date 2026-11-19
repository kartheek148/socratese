from dataclasses import dataclass
from typing import Protocol

from experience_engine.domain.trace import ArtifactRef


@dataclass(frozen=True)
class StoredArtifact:
    ref: ArtifactRef
    uri: str


class ArtifactNotFoundError(Exception):
    pass


class ArtifactStore(Protocol):
    """Tenant-scoped, content-addressed blob store. A tenant can never read another tenant's
    blob even when it knows the hash.
    """

    def put(
        self,
        tenant_id: str,
        data: bytes,
        *,
        role: str,
        content_type: str,
        redacted: bool = False,
        truncated: bool = False,
    ) -> StoredArtifact: ...

    def get(self, tenant_id: str, sha256: str) -> bytes:
        """Raises ArtifactNotFoundError; verifies the content hash on read."""
        ...
