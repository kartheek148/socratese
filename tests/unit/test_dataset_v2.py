"""Dataset v2 (harder, shared-evidence tasks): every answer must be derivable from the files alone,
and the obvious shortcuts must fail the independent verifier."""

import json
import re
from pathlib import Path

import pytest

from experience_engine.bench.dataset import (
    DEFAULT_SEED,
    DatasetLoader,
    generate_split,
    write_dataset,
    write_private_dataset,
)
from experience_engine.bench.environment import SimulatedEnvironment, get_dotted
from experience_engine.bench.verifier import SimulationVerifier
from experience_engine.domain.benchmark import Split
from experience_engine.domain.outcome import OutcomeStatus, aggregate_status

COMMITTED_V2 = Path(__file__).resolve().parents[2] / "benchmarks" / "v2"
ALL_TASKS = [t for s in Split for t in generate_split(s, DEFAULT_SEED, "v2")]


def _doc(files: dict[str, str], path: str) -> dict:  # type: ignore[type-arg]
    return json.loads(files[path])


def _config_path(files: dict[str, str]) -> str:
    return next(p for p in files if not p.startswith(("logs/", "infra/")))


def _verify(bench, edits: list[tuple[str, str, object]]) -> OutcomeStatus:  # type: ignore[no-untyped-def]
    session = SimulatedEnvironment(bench.environment, bench.task.allowed_tools)
    for path, key, value in edits:
        res = session.tools.call("set_json_value", {"path": path, "key": key, "value": value})
        assert res.ok, res.output
    return aggregate_status(SimulationVerifier().verify(bench.task, dict(session.snapshot())))


def _pool_facts(bench) -> dict:  # type: ignore[no-untyped-def,type-arg]
    """What a careful engineer derives from the environment only (no criteria, no task spec)."""
    files = bench.environment.files
    path = _config_path(files)
    cfg = _doc(files, path)
    if "database" in cfg:
        w, t = cfg["server"]["workers"], cfg["server"]["threads_per_worker"]
        overflow = cfg["database"]["max_overflow"]
        key, offset = "database.pool_size", overflow
    elif "db" in cfg:
        w, t, key, offset = cfg["workers"], cfg["threads"], "db.max_size", 0
    else:
        w, t = 1, cfg["server"]["tomcat"]["threads"]["max"]
        key, offset = "spring.datasource.hikari.maximumPoolSize", 0
    per_request = int(re.search(r"db_connections=(\d+)", files["logs/app.log"]).group(1))  # type: ignore[union-attr]
    infra = _doc(files, "infra/db.json")
    others = sum(infra["other_services_connections"].values())
    usable = infra["max_connections"] - infra["reserved_connections"] - others
    return {
        "path": path,
        "key": key,
        "offset": offset,
        "demand": w * t * per_request,
        "ceiling": usable // infra["service_replicas"],
        "whole_db": infra["max_connections"] - infra["reserved_connections"],
        "naive_demand": w * t,
    }


def _timeout_facts(bench) -> dict:  # type: ignore[no-untyped-def,type-arg]
    files = bench.environment.files
    path = _config_path(files)
    cfg = _doc(files, path)
    key = next(
        k
        for k in (
            "http.timeout_ms",
            "upstream.read_timeout_s",
            "okhttp.readTimeoutMillis",
        )
        if get_dotted(cfg, k)[0]
    )
    unit = 1000 if key.endswith("_s") else 1
    retries = next(
        v
        for k in ("http.retries", "upstream.retries", "okhttp.maxRetries")
        for found, v in [get_dotted(cfg, k)]
        if found
    )
    seen: list[float] = []
    decoys: list[float] = []
    for line in files["logs/app.log"].splitlines():
        m = re.search(r"latency_ms=(\d+)", line)
        ms = float(m.group(1)) if m else None
        if ms is None and (s := re.search(r"took=([\d.]+)s", line)):
            ms = float(s.group(1)) * 1000
        if ms is not None:
            (seen if "client=inventory" in line else decoys).append(ms)
    gateway = _doc(files, "infra/gateway.json")
    return {
        "path": path,
        "key": key,
        "unit": unit,
        "need": max(seen),
        "decoy": max(decoys),
        "per_attempt_cap": min(
            gateway["max_request_ms"] // (retries + 1),
            int(max(seen) * gateway["max_timeout_to_slowest_call_ratio"]),
        ),
        "budget_only_cap": gateway["max_request_ms"] // (retries + 1),
        "gateway": gateway["max_request_ms"],
    }


@pytest.mark.parametrize(
    "bench", [t for t in ALL_TASKS if t.family == "pool"], ids=lambda b: b.task.task_id
)
def test_pool_tasks_are_solvable_from_the_files_and_shortcuts_fail(bench) -> None:  # type: ignore[no-untyped-def]
    f = _pool_facts(bench)
    assert f["demand"] <= f["ceiling"] < f["whole_db"]
    at = lambda capacity: [(f["path"], f["key"], capacity - f["offset"])]  # noqa: E731
    assert _verify(bench, at(f["demand"])) == OutcomeStatus.VERIFIED_SUCCESS
    assert _verify(bench, at(f["ceiling"])) == OutcomeStatus.VERIFIED_SUCCESS
    # the shortcuts that work on dataset v1 do not work here
    assert _verify(bench, at(f["naive_demand"])) == OutcomeStatus.FAILURE  # ignores db_connections
    assert _verify(bench, at(f["ceiling"] + 1)) == OutcomeStatus.FAILURE  # steals from others
    assert _verify(bench, at(f["whole_db"])) == OutcomeStatus.FAILURE  # sizes to the whole database
    assert _verify(bench, []) == OutcomeStatus.FAILURE  # the starting state is broken


@pytest.mark.parametrize(
    "bench", [t for t in ALL_TASKS if t.family == "timeout"], ids=lambda b: b.task.task_id
)
def test_timeout_tasks_are_solvable_from_the_files_and_shortcuts_fail(bench) -> None:  # type: ignore[no-untyped-def]
    f = _timeout_facts(bench)
    assert f["need"] < f["per_attempt_cap"] < f["decoy"]
    at = lambda ms: [(f["path"], f["key"], ms / f["unit"])]  # noqa: E731
    assert _verify(bench, at(f["need"])) == OutcomeStatus.VERIFIED_SUCCESS
    assert _verify(bench, at(f["per_attempt_cap"])) == OutcomeStatus.VERIFIED_SUCCESS
    assert _verify(bench, at(f["decoy"])) == OutcomeStatus.FAILURE  # sized to another client
    assert _verify(bench, at(f["gateway"])) == OutcomeStatus.FAILURE  # ignores retries
    # "use everything the budget allows" only works when the budget is the binding limit
    greedy = _verify(bench, at(f["budget_only_cap"]))
    assert (greedy == OutcomeStatus.VERIFIED_SUCCESS) == (
        f["budget_only_cap"] == f["per_attempt_cap"]
    )
    assert _verify(bench, at(f["per_attempt_cap"] + 1)) == OutcomeStatus.FAILURE
    assert _verify(bench, at(f["need"] - 1)) == OutcomeStatus.FAILURE
    assert _verify(bench, []) == OutcomeStatus.FAILURE


def test_v2_shape_is_larger_than_v1_and_hidden_uses_unseen_variants() -> None:
    sizes = {s: len(generate_split(s, DEFAULT_SEED, "v2")) for s in Split}
    assert sizes == {Split.DISCOVERY: 16, Split.VALIDATION: 12, Split.HIDDEN_TRANSFER: 12}
    variants = {s: {t.variant for t in generate_split(s, DEFAULT_SEED, "v2")} for s in Split}
    assert not variants[Split.HIDDEN_TRANSFER] & variants[Split.DISCOVERY]
    assert not variants[Split.VALIDATION] - variants[Split.DISCOVERY]
    ids = [t.task.task_id for t in ALL_TASKS]
    assert len(ids) == len(set(ids))


def test_v2_is_deterministic_seed_sensitive_and_independent_of_v1() -> None:
    a = [t.model_dump_json() for t in generate_split(Split.DISCOVERY, DEFAULT_SEED, "v2")]
    assert a == [t.model_dump_json() for t in generate_split(Split.DISCOVERY, DEFAULT_SEED, "v2")]
    assert a != [
        t.model_dump_json() for t in generate_split(Split.DISCOVERY, DEFAULT_SEED + 1, "v2")
    ]
    v1 = {t.task.task_id for t in generate_split(Split.DISCOVERY, DEFAULT_SEED)}
    assert not v1 & {t.task.task_id for t in generate_split(Split.DISCOVERY, DEFAULT_SEED, "v2")}


def test_committed_v2_matches_generator(tmp_path: Path) -> None:
    write_dataset(tmp_path, DEFAULT_SEED, "v2")
    assert (tmp_path / "manifest.json").read_text() == (COMMITTED_V2 / "manifest.json").read_text()
    for split in Split:
        for f in sorted((tmp_path / split.value.lower()).glob("*.json")):
            assert f.read_text() == (COMMITTED_V2 / split.value.lower() / f.name).read_text()


def test_private_v2_is_versioned_and_loadable(tmp_path: Path) -> None:
    from experience_engine.bench.dataset import EvaluatorAccess

    manifest = write_private_dataset(tmp_path, seed=424242, dataset="v2")
    assert manifest["version"] == "2.0.0-private" and "seed" not in manifest
    loaded = DatasetLoader(COMMITTED_V2, EvaluatorAccess(), private_root=tmp_path)
    assert loaded.load(Split.HIDDEN_TRANSFER).version.n_tasks == 12
    assert loaded.load(Split.VALIDATION).version.version == "2.0.0-private"
