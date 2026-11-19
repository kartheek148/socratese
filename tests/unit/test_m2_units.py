import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from experience_engine.adapters.artifacts.local_fs import LocalFsArtifactStore
from experience_engine.bench.verifier import SimulationVerifier
from experience_engine.domain.ids import new_id
from experience_engine.domain.outcome import OutcomeStatus
from experience_engine.domain.redaction import REDACTED, Redactor
from experience_engine.domain.retry import RetryPolicy
from experience_engine.domain.run import ExecutionCompletedPayload
from experience_engine.domain.trace import TraceEvent, TraceEventType
from experience_engine.learning.collector import ExecutionCompletedHandler, NonRetryableEventError
from experience_engine.ports.artifacts import ArtifactNotFoundError
from experience_engine.ports.learning import EnqueueResult
from support import (
    FakeClock,
    InMemoryLifecycle,
    ScriptedLLM,
    correct_pool_fix,
    first_task,
    orchestrator,
    persisted_orchestrator,
    run_persisted,
    session,
)


# ---- redaction -------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "key sk-abcdefghijklmnopqrstuvwx",
        "token ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "Authorization: Bearer abcdefghijklmnop.qrstuv",
        "aws AKIAABCDEFGHIJKLMNOP",
        "postgresql://app:s3cretpw@db.internal/app",
        '{"db_password": "hunter2hunter2"}',
        "api_key=abc123def456",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----",
    ],
)
def test_redactor_masks_common_secret_shapes(text: str) -> None:
    result = Redactor().redact(text)
    assert result.redacted and REDACTED in result.text
    for leaked in (
        "abcdefghijklmnopqrstuvwx",
        "s3cretpw",
        "hunter2hunter2",
        "abc123def456",
        "MIIabc",
    ):
        assert leaked not in result.text


def test_redactor_known_secrets_and_clean_text() -> None:
    r = Redactor(known_secrets=["correct-horse-battery", "ab"])
    assert "correct-horse" not in r.redact_text("x correct-horse-battery y")
    clean = r.redact("pool_size=10 timeout_ms=3000 about")
    assert not clean.redacted and clean.text == "pool_size=10 timeout_ms=3000 about"


# ---- retry policy ----------------------------------------------------------------------------
def test_retry_policy_is_bounded_exponential_with_jitter() -> None:
    p = RetryPolicy(max_attempts=4, base_delay_s=1.0, max_delay_s=5.0)
    assert [p.delay_s(n, lambda: 1.0) for n in (1, 2, 3, 4, 5)] == [1.0, 2.0, 4.0, 5.0, 5.0]
    assert p.delay_s(3, lambda: 0.0) == 0.0
    assert not p.exhausted(3) and p.exhausted(4)


# ---- trace event bounds ----------------------------------------------------------------------
def _event(**over: object) -> TraceEvent:
    base = dict(
        event_id=new_id(),
        tenant_id="t",
        task_id="k",
        run_id=new_id(),
        trace_id="x",
        sequence=0,
        type=TraceEventType.DECISION,
        component="c",
        component_version="1",
        action="a",
        status="s",
        ts=datetime.now(UTC),
    )
    return TraceEvent(**{**base, **over})  # type: ignore[arg-type]


def test_trace_event_rejects_unbounded_metrics_and_naive_time() -> None:
    _event(metrics={"ok": "x" * 256})
    with pytest.raises(ValidationError):
        _event(metrics={"big": "x" * 257})
    with pytest.raises(ValidationError):
        _event(metrics={f"k{i}": i for i in range(33)})
    with pytest.raises(ValidationError):
        _event(ts=datetime(2025, 1, 1))


# ---- local artifact store --------------------------------------------------------------------
def test_artifact_store_is_content_addressed_and_tenant_scoped(tmp_path: Path) -> None:
    store = LocalFsArtifactStore(tmp_path)
    a = store.put("tenant-a", b"hello", role="r", content_type="text/plain")
    assert store.put("tenant-a", b"hello", role="r", content_type="text/plain").ref == a.ref
    assert store.get("tenant-a", a.ref.sha256) == b"hello"
    with pytest.raises(ArtifactNotFoundError):
        store.get("tenant-b", a.ref.sha256)


def test_artifact_store_rejects_traversal_and_tampering(tmp_path: Path) -> None:
    store = LocalFsArtifactStore(tmp_path / "root")
    evil = store.put("../../etc", b"x", role="r", content_type="text/plain")
    assert (tmp_path / "root").resolve() in next(
        (tmp_path).rglob(evil.ref.sha256)
    ).resolve().parents
    for bad in ("../x", "A" * 64, "short", ""):
        with pytest.raises(ArtifactNotFoundError):
            store.get("t", bad)
    stored = store.put("t", b"original", role="r", content_type="text/plain")
    path = next((tmp_path / "root").rglob(stored.ref.sha256))
    path.write_bytes(b"tampered")
    with pytest.raises(ArtifactNotFoundError):
        store.get("t", stored.ref.sha256)


# ---- recorder / orchestrator wiring (fakes) --------------------------------------------------
def test_orchestrator_emits_ordered_bounded_events_and_one_outbox_event(tmp_path: Path) -> None:
    task = first_task()
    lc = InMemoryLifecycle()
    result = run_persisted(task, correct_pool_fix(task), lc, LocalFsArtifactStore(tmp_path))
    types = [e.type.value for e in lc.events]  # type: ignore[attr-defined]
    assert types == [
        "DECISION",
        "MODEL_CALL",
        "TOOL_CALL",
        "MODEL_CALL",
        "RUN_TERMINATED",
        "VERIFICATION",
    ]
    assert [e.sequence for e in lc.events] == list(range(6))  # type: ignore[attr-defined]
    ids = {e.event_id: e for e in lc.events}  # type: ignore[attr-defined]
    tool = lc.events[2]  # type: ignore[attr-defined]
    assert ids[tool.parent_event_id].type == TraceEventType.MODEL_CALL
    assert {r.role for r in tool.artifact_refs} == {"args", "output"}
    ((outcome, decision, _ended, outbox),) = lc.completed  # type: ignore[misc]
    assert outcome.status == OutcomeStatus.VERIFIED_SUCCESS == decision.outcome_status
    payload = ExecutionCompletedPayload.model_validate(outbox.payload)
    assert payload.event_id == outbox.event_id and payload.run_id == result.run_id
    assert outbox.correlation_id == result.run_id


def test_recorder_redacts_payloads_and_metrics(tmp_path: Path) -> None:
    task = first_task()
    lc = InMemoryLifecycle()
    store = LocalFsArtifactStore(tmp_path)
    secret = "my-llm-key-0123456789"
    replies = [
        {"tool": "read_file", "args": {"path": "app/config.json"}},
        {"final": f"done password=hunter2hunter2 key {secret}"},
    ]
    run_persisted(task, replies, lc, store, redactor=Redactor(known_secrets=[secret]))
    blobs = []
    for tenant, ref, _uri in lc.artifacts:
        blobs.append(store.get(tenant, ref.sha256).decode())  # type: ignore[attr-defined]
    joined = "\n".join(blobs) + json.dumps([e.model_dump(mode="json") for e in lc.events])  # type: ignore[attr-defined]
    assert secret not in joined and "hunter2hunter2" not in joined and REDACTED in joined
    assert any(ref.redacted for _t, ref, _u in lc.artifacts)  # type: ignore[attr-defined]


def test_without_persistence_behaviour_is_unchanged_and_requires_both_ports() -> None:
    task = first_task()
    clock = FakeClock()
    with pytest.raises(ValueError):
        persisted_orchestrator(clock, InMemoryLifecycle(), None)
    run = orchestrator(clock, SimulationVerifier()).execute(
        task.task, session(task), ScriptedLLM(correct_pool_fix(task), clock)
    )
    assert run.outcome.status == OutcomeStatus.VERIFIED_SUCCESS


# ---- idempotent handler (fake store) ---------------------------------------------------------
class FakeStore:
    def __init__(self) -> None:
        self.consumed: set[tuple[str, str]] = set()
        self.jobs: dict[tuple[str, str, str], object] = {}
        self.dead: list[dict[str, object]] = []

    def register_and_enqueue(self, consumer, event, job):  # type: ignore[no-untyped-def]
        if (consumer, event.event_id) in self.consumed:
            return EnqueueResult.DUPLICATE_DELIVERY
        self.consumed.add((consumer, event.event_id))
        key = (job.tenant_id, job.job_type, job.dedupe_key)
        if key in self.jobs:
            return EnqueueResult.DUPLICATE_JOB
        self.jobs[key] = job
        return EnqueueResult.ENQUEUED

    def record_dead_letter(self, **kw: object) -> None:
        self.dead.append(kw)


def _payload(
    status: OutcomeStatus = OutcomeStatus.VERIFIED_SUCCESS, **over: object
) -> dict[str, object]:
    base = ExecutionCompletedPayload(
        event_id=new_id(),
        tenant_id="t",
        run_id=new_id(),
        task_id="k",
        correlation_id="c",
        outcome_status=status,
        config_id="A",
        policy_version="p",
        verifier_version="v",
        occurred_at=datetime.now(UTC),
    ).model_dump(mode="json")
    return {**base, **over}


def test_handler_is_idempotent_for_redelivery_and_duplicate_events() -> None:
    store, handler = FakeStore(), ExecutionCompletedHandler(FakeStore(), FakeClock())
    handler = ExecutionCompletedHandler(store, FakeClock())
    msg = _payload()
    assert handler.handle(msg) == EnqueueResult.ENQUEUED
    assert handler.handle(msg) == EnqueueResult.DUPLICATE_DELIVERY
    again = {**msg, "event_id": new_id()}  # same run, new event id
    assert handler.handle(again) == EnqueueResult.DUPLICATE_JOB
    assert len(store.jobs) == 1


@pytest.mark.parametrize(
    ("status", "eligible"),
    [
        (OutcomeStatus.VERIFIED_SUCCESS, True),
        (OutcomeStatus.FAILURE, False),
        (OutcomeStatus.PARTIAL, False),
        (OutcomeStatus.UNKNOWN, False),
    ],
)
def test_only_verified_success_is_eligible_for_success_mining(
    status: OutcomeStatus, eligible: bool
) -> None:
    store = FakeStore()
    ExecutionCompletedHandler(store, FakeClock()).handle(_payload(status))
    (job,) = store.jobs.values()
    assert job.eligible_for_success_mining is eligible and job.outcome_status == status  # type: ignore[attr-defined]


def test_handler_rejects_poison_payloads() -> None:
    handler = ExecutionCompletedHandler(FakeStore(), FakeClock())
    for bad in (
        {},
        _payload(outcome_status="WINNING"),
        _payload(schema_version="x"),
        {**_payload(), "extra": 1},
    ):
        with pytest.raises(NonRetryableEventError):
            handler.handle(bad)


def test_dead_letter_tolerates_garbage_identifiers() -> None:
    store = FakeStore()
    handler = ExecutionCompletedHandler(store, FakeClock())
    handler.dead_letter({"event_id": "not-a-uuid", "tenant_id": 7}, "bad", 1, "{}")
    handler.dead_letter({"event_id": new_id(), "tenant_id": "acme"}, "bad", 2, "{}")
    assert [d["event_id"] is None for d in store.dead] == [True, False]
    assert store.dead[0]["tenant_id"] is None and store.dead[1]["tenant_id"] == "acme"
