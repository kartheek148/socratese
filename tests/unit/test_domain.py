import re
import uuid

from experience_engine.domain.ids import new_id
from experience_engine.domain.outcome import (
    OutcomeStatus,
    VerificationResult,
    aggregate_status,
    quality_score,
)


def _r(passed: bool | None, role: str = "goal") -> VerificationResult:
    return VerificationResult(criterion_kind="k", role=role, passed=passed, detail="d")  # type: ignore[arg-type]


def test_uuid7_format_and_ordering() -> None:
    a, b = new_id(now_ms=1_700_000_000_000), new_id(now_ms=1_700_000_000_001)
    for value in (a, b):
        assert uuid.UUID(value).version == 7
        assert re.fullmatch(r"[0-9a-f-]{36}", value)
    assert a < b
    assert a != new_id(now_ms=1_700_000_000_000)


def test_aggregate_status_rules() -> None:
    assert aggregate_status([_r(True), _r(True, "guard")]) == OutcomeStatus.VERIFIED_SUCCESS
    assert aggregate_status([_r(False), _r(True, "guard")]) == OutcomeStatus.FAILURE
    assert aggregate_status([_r(True), _r(False, "guard")]) == OutcomeStatus.FAILURE
    assert aggregate_status([_r(True), _r(False)]) == OutcomeStatus.PARTIAL
    assert aggregate_status([_r(True), _r(None)]) == OutcomeStatus.UNKNOWN
    assert aggregate_status([]) == OutcomeStatus.UNKNOWN


def test_quality_score() -> None:
    assert quality_score([_r(True), _r(False)]) == 0.5
    assert quality_score([_r(True), _r(False, "guard")]) == 0.0
    assert quality_score([_r(None)]) is None
