import json

import pytest

from experience_engine.bench.verifier import SimulationVerifier
from experience_engine.domain.outcome import OutcomeStatus, aggregate_status
from experience_engine.ports.tools import ToolNotAllowedError
from support import correct_pool_fix, first_task, session

verifier = SimulationVerifier()


def _status(task_name: str, files: dict[str, str]) -> OutcomeStatus:
    task = (
        first_task("pool", "sqlalchemy") if task_name == "pool" else first_task("timeout", "httpx")
    )
    return aggregate_status(verifier.verify(task.task, files))


def test_untouched_environment_fails() -> None:
    task = first_task()
    assert (
        aggregate_status(verifier.verify(task.task, task.environment.files))
        == OutcomeStatus.FAILURE
    )


def test_correct_fix_verifies() -> None:
    task = first_task()
    env = session(task)
    for action in correct_pool_fix(task)[:-1]:
        assert isinstance(action, dict)
        assert env.tools.call(action["tool"], action["args"]).ok
    assert (
        aggregate_status(verifier.verify(task.task, env.snapshot()))
        == OutcomeStatus.VERIFIED_SUCCESS
    )


def test_guard_violation_fails_even_if_goal_met() -> None:
    task = first_task()
    files = dict(task.environment.files)
    doc = json.loads(files["app/config.json"])
    doc["database"]["pool_size"] = 10_000
    files["app/config.json"] = json.dumps(doc)
    infra = json.loads(files["infra/db.json"])
    infra["max_connections"] = 100_000
    files["infra/db.json"] = json.dumps(infra)
    results = verifier.verify(task.task, files)
    assert aggregate_status(results) == OutcomeStatus.FAILURE
    assert any(r.role == "guard" and r.passed is False for r in results)


def test_reducing_concurrency_is_rejected() -> None:
    task = first_task()
    files = dict(task.environment.files)
    doc = json.loads(files["app/config.json"])
    doc["server"]["workers"] = 1
    doc["server"]["threads_per_worker"] = 1
    doc["database"]["pool_size"] = 2
    files["app/config.json"] = json.dumps(doc)
    assert aggregate_status(verifier.verify(task.task, files)) != OutcomeStatus.VERIFIED_SUCCESS


def test_corrupt_or_deleted_config_is_failure_not_success() -> None:
    task = first_task()
    corrupt = {**task.environment.files, "app/config.json": "{not json"}
    assert aggregate_status(verifier.verify(task.task, corrupt)) == OutcomeStatus.FAILURE
    deleted = {k: v for k, v in task.environment.files.items() if k != "app/config.json"}
    assert aggregate_status(verifier.verify(task.task, deleted)) == OutcomeStatus.FAILURE


def test_missing_snapshot_is_unknown() -> None:
    task = first_task()
    assert aggregate_status(verifier.verify(task.task, None)) == OutcomeStatus.UNKNOWN


def test_timeout_task_requires_covering_worst_latency() -> None:
    task = first_task("timeout", "httpx")
    env = session(task)
    assert env.tools.call(
        "set_json_value", {"path": "client/config.json", "key": "http.timeout_ms", "value": 1}
    ).ok
    assert aggregate_status(verifier.verify(task.task, env.snapshot())) == OutcomeStatus.FAILURE
    assert env.tools.call(
        "set_json_value", {"path": "client/config.json", "key": "http.timeout_ms", "value": 9000}
    ).ok
    assert (
        aggregate_status(verifier.verify(task.task, env.snapshot()))
        == OutcomeStatus.VERIFIED_SUCCESS
    )


def test_tool_gateway_allowlist_and_validation() -> None:
    task = first_task()
    env = session(task)
    with pytest.raises(ToolNotAllowedError):
        env.tools.call("write_file", {"path": "app/config.json", "content": "x"})
    with pytest.raises(ToolNotAllowedError):
        env.tools.call("rm", {})
    assert not env.tools.call("read_file", {"path": "../etc/passwd"}).ok
    assert not env.tools.call("read_file", {"path": "/etc/passwd"}).ok
    assert not env.tools.call("read_file", {"path": 5}).ok
    assert not env.tools.call(
        "set_json_value", {"path": "app/config.json", "key": "nope.nope", "value": 1}
    ).ok


def test_environments_are_isolated() -> None:
    task = first_task()
    a, b = session(task), session(task)
    a.tools.call(
        "set_json_value", {"path": "app/config.json", "key": "database.pool_size", "value": 99}
    )
    assert b.snapshot() == task.environment.files
    assert a.snapshot() != b.snapshot()
