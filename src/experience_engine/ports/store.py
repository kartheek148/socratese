from typing import Protocol

from experience_engine.domain.benchmark import BenchmarkRunRecord


class ResultStore(Protocol):
    """Append-only store of run records. Reads are always tenant-scoped."""

    def save_run(self, record: BenchmarkRunRecord) -> None: ...

    def list_runs(self, tenant_id: str) -> list[BenchmarkRunRecord]: ...
