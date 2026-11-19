import hashlib
import os
import re
import tempfile
from pathlib import Path

from experience_engine.domain.trace import ArtifactRef
from experience_engine.ports.artifacts import ArtifactNotFoundError, StoredArtifact

_SHA = re.compile(r"^[0-9a-f]{64}$")


class LocalFsArtifactStore:
    """Content-addressed files under root/<sha256(tenant)>/<sha[:2]>/<sha>.

    The tenant id is hashed for the directory name, so no tenant-controlled string ever reaches
    a path. Reads re-hash the content and refuse tampered files.
    """

    def __init__(self, root: Path):
        self._root = root
        root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _dir(self, tenant_id: str) -> str:
        return hashlib.sha256(tenant_id.encode()).hexdigest()[:32]

    def _path(self, tenant_id: str, sha256: str) -> Path:
        if not _SHA.fullmatch(sha256):
            raise ArtifactNotFoundError("invalid artifact hash")
        return self._root / self._dir(tenant_id) / sha256[:2] / sha256

    def put(
        self,
        tenant_id: str,
        data: bytes,
        *,
        role: str,
        content_type: str,
        redacted: bool = False,
        truncated: bool = False,
    ) -> StoredArtifact:
        sha = hashlib.sha256(data).hexdigest()
        path = self._path(tenant_id, sha)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, tmp = tempfile.mkstemp(dir=path.parent)
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(data)
                os.replace(tmp, path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
        ref = ArtifactRef(
            role=role,
            sha256=sha,
            size_bytes=len(data),
            content_type=content_type,
            redacted=redacted,
            truncated=truncated,
        )
        return StoredArtifact(ref=ref, uri=f"local://{self._dir(tenant_id)}/{sha}")

    def get(self, tenant_id: str, sha256: str) -> bytes:
        path = self._path(tenant_id, sha256)
        try:
            data = path.read_bytes()
        except FileNotFoundError as exc:
            raise ArtifactNotFoundError("artifact not found") from exc
        if hashlib.sha256(data).hexdigest() != sha256:
            raise ArtifactNotFoundError("artifact content does not match its hash")
        return data
