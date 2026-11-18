import json
from collections.abc import Mapping

from experience_engine.bench.environment import get_dotted
from experience_engine.domain.outcome import VerificationResult
from experience_engine.domain.task import (
    PoolCapacityCriterion,
    TaskSpec,
    TimeoutCoversLatencyCriterion,
    ValueUnchangedCriterion,
)

VERIFIER_VERSION = "sim-verifier/1"


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


class SimulationVerifier:
    """Evaluates success criteria against the final environment snapshot."""

    version = VERIFIER_VERSION

    def verify(
        self, task: TaskSpec, snapshot: Mapping[str, str] | None
    ) -> list[VerificationResult]:
        if snapshot is None:
            return [
                VerificationResult(
                    criterion_kind="environment_evidence",
                    role="goal",
                    passed=None,
                    detail="environment snapshot unavailable",
                )
            ]
        results: list[VerificationResult] = []
        for crit in task.success_criteria:
            if isinstance(crit, PoolCapacityCriterion):
                results.append(self._pool(crit, snapshot))
            elif isinstance(crit, TimeoutCoversLatencyCriterion):
                results.append(self._timeout(crit, snapshot))
            else:
                results.append(self._unchanged(crit, snapshot))
        return results

    @staticmethod
    def _load(path: str, snapshot: Mapping[str, str]) -> tuple[object | None, str | None]:
        if path not in snapshot:
            return None, f"{path} is missing"
        try:
            return json.loads(snapshot[path]), None
        except json.JSONDecodeError:
            return None, f"{path} is not valid JSON"

    def _pool(self, c: PoolCapacityCriterion, snap: Mapping[str, str]) -> VerificationResult:
        doc, err = self._load(c.config_path, snap)
        if err:
            return VerificationResult(criterion_kind=c.kind, role=c.role, passed=False, detail=err)
        total = 0.0
        for key in c.capacity_keys:
            found, raw = get_dotted(doc, key)
            num = _number(raw) if found else None
            if num is None or num < 0:
                return VerificationResult(
                    criterion_kind=c.kind,
                    role=c.role,
                    passed=False,
                    detail=f"{key} missing or invalid",
                )
            total += num
        ok = c.min_capacity <= total <= c.max_capacity
        return VerificationResult(
            criterion_kind=c.kind,
            role=c.role,
            passed=ok,
            detail=f"capacity={total:g} required in [{c.min_capacity}, {c.max_capacity}]",
        )

    def _timeout(
        self, c: TimeoutCoversLatencyCriterion, snap: Mapping[str, str]
    ) -> VerificationResult:
        doc, err = self._load(c.config_path, snap)
        if err:
            return VerificationResult(criterion_kind=c.kind, role=c.role, passed=False, detail=err)
        found, raw = get_dotted(doc, c.timeout_key)
        num = _number(raw) if found else None
        if num is None or num <= 0:
            return VerificationResult(
                criterion_kind=c.kind,
                role=c.role,
                passed=False,
                detail=f"{c.timeout_key} missing or invalid",
            )
        timeout_ms = num * c.unit_ms
        need = max(c.latency_samples_ms)
        ok = need <= timeout_ms <= c.max_timeout_ms
        return VerificationResult(
            criterion_kind=c.kind,
            role=c.role,
            passed=ok,
            detail=f"timeout_ms={timeout_ms:g} must cover {need} and stay <= {c.max_timeout_ms}",
        )

    def _unchanged(self, c: ValueUnchangedCriterion, snap: Mapping[str, str]) -> VerificationResult:
        doc, err = self._load(c.path, snap)
        if err:
            return VerificationResult(criterion_kind=c.kind, role=c.role, passed=False, detail=err)
        found, raw = get_dotted(doc, c.key)
        ok = found and raw == c.expected and type(raw) is type(c.expected)
        return VerificationResult(
            criterion_kind=c.kind,
            role=c.role,
            passed=ok,
            detail=f"{c.path}:{c.key} {'unchanged' if ok else 'was modified or removed'}",
        )
