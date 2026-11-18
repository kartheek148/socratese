from pathlib import Path

from experience_engine.domain.benchmark import BenchmarkRunRecord


class InMemoryResultStore:
    def __init__(self) -> None:
        self._records: list[BenchmarkRunRecord] = []

    def save_run(self, record: BenchmarkRunRecord) -> None:
        self._records.append(record)

    def list_runs(self, tenant_id: str) -> list[BenchmarkRunRecord]:
        return [r for r in self._records if r.tenant_id == tenant_id]


class JsonlResultStore:
    """Append-only JSON Lines store; one record per line, never rewritten."""

    def __init__(self, path: Path):
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def save_run(self, record: BenchmarkRunRecord) -> None:
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(record.model_dump_json() + "\n")

    def list_runs(self, tenant_id: str) -> list[BenchmarkRunRecord]:
        if not self._path.exists():
            return []
        records = [
            BenchmarkRunRecord.model_validate_json(line)
            for line in self._path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        return [r for r in records if r.tenant_id == tenant_id]
