"""Dataset v3: the hidden-only `queue` family must be solvable from files alone, its shortcuts must
fail, and it must never appear in discovery (so no learning component can see it)."""

import json
import re

import pytest

from experience_engine.bench.dataset import DEFAULT_SEED, generate_split
from experience_engine.bench.environment import SimulatedEnvironment
from experience_engine.bench.verifier import SimulationVerifier
from experience_engine.domain.benchmark import Split
from experience_engine.domain.outcome import OutcomeStatus, aggregate_status

QUEUE = [
    t for t in generate_split(Split.HIDDEN_TRANSFER, DEFAULT_SEED, "v3") if t.family == "queue"
]
PATH, KEY = "messaging/consumer.json", "broker.channel_pool.size"


def _verify(bench, size: int | None) -> OutcomeStatus:  # type: ignore[no-untyped-def]
    session = SimulatedEnvironment(bench.environment, bench.task.allowed_tools)
    if size is not None:
        res = session.tools.call("set_json_value", {"path": PATH, "key": KEY, "value": size})
        assert res.ok, res.output
    return aggregate_status(SimulationVerifier().verify(bench.task, dict(session.snapshot())))


def _facts(bench) -> dict[str, int]:  # type: ignore[no-untyped-def]
    f = bench.environment.files
    cfg, infra = json.loads(f[PATH]), json.loads(f["infra/broker.json"])
    per = int(re.search(r"channels_per_message=(\d+)", f["logs/app.log"]).group(1))  # type: ignore[union-attr]
    usable = (
        infra["max_channels"]
        - infra["reserved_channels"]
        - sum(infra["other_services_channels"].values())
    )
    return {
        "demand": cfg["consumer"]["instances"] * cfg["consumer"]["concurrency"] * per,
        "naive": cfg["consumer"]["instances"] * cfg["consumer"]["concurrency"],
        "ceiling": usable // infra["service_replicas"],
        "whole": infra["max_channels"] - infra["reserved_channels"],
    }


def test_v3_adds_queue_only_to_hidden_transfer_and_keeps_other_splits_complete() -> None:
    assert len(QUEUE) == 6
    for split in (Split.DISCOVERY, Split.VALIDATION):
        assert "queue" not in {t.family for t in generate_split(split, DEFAULT_SEED, "v3")}
    hidden = generate_split(Split.HIDDEN_TRANSFER, DEFAULT_SEED, "v3")
    assert {t.family for t in hidden} == {"pool", "timeout", "queue"}
    ids = [t.task.task_id for s in Split for t in generate_split(s, DEFAULT_SEED, "v3")]
    assert len(ids) == len(set(ids))


def test_v3_does_not_reuse_v2_instances() -> None:
    v2 = {t.task.task_id: t.model_dump_json() for t in generate_split(Split.VALIDATION, 5, "v2")}
    v3 = {t.task.task_id: t.model_dump_json() for t in generate_split(Split.VALIDATION, 5, "v3")}
    assert v2.keys() == v3.keys() and any(v2[k] != v3[k] for k in v2)


@pytest.mark.parametrize("bench", QUEUE, ids=lambda b: b.task.task_id)
def test_queue_tasks_are_solvable_from_the_files_and_shortcuts_fail(bench) -> None:  # type: ignore[no-untyped-def]
    f = _facts(bench)
    assert f["demand"] <= f["ceiling"] < f["whole"]
    assert _verify(bench, f["demand"]) == OutcomeStatus.VERIFIED_SUCCESS
    assert _verify(bench, f["ceiling"]) == OutcomeStatus.VERIFIED_SUCCESS
    assert _verify(bench, f["naive"]) == OutcomeStatus.FAILURE  # ignores channels per message
    assert _verify(bench, f["ceiling"] + 1) == OutcomeStatus.FAILURE
    assert _verify(bench, f["whole"]) == OutcomeStatus.FAILURE
    assert _verify(bench, None) == OutcomeStatus.FAILURE


def test_queue_vocabulary_shares_nothing_with_the_learned_pool_family() -> None:
    text = json.dumps(QUEUE[0].environment.files) + QUEUE[0].task.objective
    for word in ("pool_size", "max_connections", "database", "hikari", "sqlalchemy", "psycopg"):
        assert word not in text
