"""Versioned, content-hashed benchmark datasets with split access control.

Only DISCOVERY is available without evaluator access, so learning components (milestone 3+) are
wired with a loader that cannot read VALIDATION or HIDDEN_TRANSFER. This is an application-level
guard, not a security boundary: the files live in the same repository. Before milestone 3 the
hidden split must move to storage the learning plane cannot read (see docs/benchmark.md).
"""

import hashlib
import json
import os
import random
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from experience_engine.bench.environment import EnvironmentSpec
from experience_engine.bench.families import (
    make_pool_task,
    make_timeout_task,
    rng_for,
)
from experience_engine.bench.families_v2 import make_budget_timeout_task, make_shared_pool_task
from experience_engine.domain.benchmark import DatasetVersion, Split
from experience_engine.domain.task import TaskSpec

DATASET_NAME = "connection-debug-sim"
DATASET_VERSION = "1.0.0"
GENERATOR_VERSION = "gen/1"
DEFAULT_SEED = 20260101
EVALUATOR_ENV = "EE_EVALUATOR_ROLE"

# split -> (family, variant, count)
SPLIT_PLAN: dict[Split, list[tuple[str, str, int]]] = {
    Split.DISCOVERY: [
        ("pool", "sqlalchemy", 3),
        ("pool", "psycopg_pool", 3),
        ("timeout", "httpx", 3),
        ("timeout", "requests", 3),
    ],
    Split.VALIDATION: [
        ("pool", "sqlalchemy", 2),
        ("pool", "psycopg_pool", 2),
        ("timeout", "httpx", 2),
        ("timeout", "requests", 2),
    ],
    Split.HIDDEN_TRANSFER: [
        ("pool", "hikari", 4),
        ("timeout", "okhttp", 4),
    ],
}

DATASET_VERSION_V2 = "2.0.0"
GENERATOR_VERSION_V2 = "gen/2"
SPLIT_PLAN_V2: dict[Split, list[tuple[str, str, int]]] = {
    Split.DISCOVERY: [
        ("pool", "sqlalchemy_shared", 4),
        ("pool", "psycopg_shared", 4),
        ("timeout", "httpx_budget", 4),
        ("timeout", "requests_budget", 4),
    ],
    Split.VALIDATION: [
        ("pool", "sqlalchemy_shared", 3),
        ("pool", "psycopg_shared", 3),
        ("timeout", "httpx_budget", 3),
        ("timeout", "requests_budget", 3),
    ],
    Split.HIDDEN_TRANSFER: [
        ("pool", "hikari_shared", 6),
        ("timeout", "okhttp_budget", 6),
    ],
}
Maker = Callable[[str, str, random.Random], tuple[TaskSpec, EnvironmentSpec]]


@dataclass(frozen=True)
class DatasetSpec:
    """One dataset generation: v1 is frozen (its committed hashes must never change)."""

    key: str
    version: str
    generator_version: str
    plan: dict[Split, list[tuple[str, str, int]]]
    makers: dict[str, Maker]


DATASETS: dict[str, DatasetSpec] = {
    "v1": DatasetSpec(
        "v1",
        DATASET_VERSION,
        GENERATOR_VERSION,
        SPLIT_PLAN,
        {"pool": make_pool_task, "timeout": make_timeout_task},
    ),
    "v2": DatasetSpec(
        "v2",
        DATASET_VERSION_V2,
        GENERATOR_VERSION_V2,
        SPLIT_PLAN_V2,
        {"pool": make_shared_pool_task, "timeout": make_budget_timeout_task},
    ),
}
_PREFIX = {Split.DISCOVERY: "disc", Split.VALIDATION: "val", Split.HIDDEN_TRANSFER: "hid"}


class BenchTask(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    task: TaskSpec
    environment: EnvironmentSpec
    family: str
    variant: str


class DatasetIntegrityError(Exception):
    pass


class SplitAccessError(PermissionError):
    pass


@dataclass(frozen=True)
class EvaluatorAccess:
    """Capability required to read VALIDATION / HIDDEN_TRANSFER. Never pass it to learning code."""

    @staticmethod
    def from_env() -> "EvaluatorAccess":
        if os.environ.get(EVALUATOR_ENV) != "evaluator":
            raise SplitAccessError(f"evaluator access requires {EVALUATOR_ENV}=evaluator")
        return EvaluatorAccess()


@dataclass(frozen=True)
class LoadedSplit:
    version: DatasetVersion
    tasks: list[BenchTask]


def _serialize(task: BenchTask) -> str:
    return json.dumps(task.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _content_hash(file_hashes: dict[str, str]) -> str:
    return _sha("\n".join(f"{name}:{file_hashes[name]}" for name in sorted(file_hashes)))


def generate_split(
    split: Split, seed: int = DEFAULT_SEED, dataset: str = "v1", scale: int = 1
) -> list[BenchTask]:
    """`scale` multiplies the tasks per variant. Task i of a scaled split equals task i of the
    unscaled one, so a larger suite extends a smaller one rather than replacing it."""
    if scale < 1:
        raise ValueError("scale must be >= 1")
    ds = DATASETS[dataset]
    tasks: list[BenchTask] = []
    for family, variant, count in ds.plan[split]:
        for i in range(count * scale):
            task_id = f"{_PREFIX[split]}-{family}-{variant}-{i:02d}"
            rng = rng_for(DATASET_NAME, ds.version, seed, split.value, family, variant, i)
            spec, env = ds.makers[family](task_id, variant, rng)
            tasks.append(BenchTask(task=spec, environment=env, family=family, variant=variant))
    return tasks


def _write_splits(
    root: Path,
    seed: int,
    splits: list[Split],
    version: str,
    record_seed: bool,
    dataset: str,
    scale: int = 1,
) -> dict[str, object]:
    manifest: dict[str, object] = {
        "dataset": DATASET_NAME,
        "version": version,
        "generator_version": DATASETS[dataset].generator_version,
        "splits": {},
    }
    if record_seed:
        manifest["seed"] = seed
    entries: dict[str, object] = {}
    for split in splits:
        directory = root / split.value.lower()
        directory.mkdir(parents=True, exist_ok=True)
        hashes: dict[str, str] = {}
        for task in generate_split(split, seed, dataset, scale):
            text = _serialize(task)
            name = f"{task.task.task_id}.json"
            (directory / name).write_text(text)
            hashes[name] = _sha(text)
        entries[split.value] = {
            "n_tasks": len(hashes),
            "content_hash": _content_hash(hashes),
            "files": hashes,
        }
    manifest["splits"] = entries
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def write_dataset(root: Path, seed: int = DEFAULT_SEED, dataset: str = "v1") -> dict[str, object]:
    """Development dataset (all splits public). Its hidden split is *burned*: the data and the
    seed are in git history, so it must only be used for baselines/regression, never as a hidden
    evaluation set. Use `write_private_dataset` for real validation/transfer evaluation.
    """
    return _write_splits(
        root, seed, list(Split), DATASETS[dataset].version, record_seed=True, dataset=dataset
    )


PRIVATE_SEED_ENV = "EE_PRIVATE_DATASET_SEED"
PRIVATE_VERSION = f"{DATASET_VERSION}-private"


def write_private_dataset(
    root: Path, seed: int, dataset: str = "v1", scale: int = 1
) -> dict[str, object]:
    """VALIDATION and HIDDEN_TRANSFER regenerated from a secret seed, written outside the repo.

    The seed is never recorded in the manifest. Task numbers therefore cannot be reproduced from
    repository contents (the families/variants remain visible in code; the values do not).
    """
    if seed == DEFAULT_SEED:
        raise ValueError("the private seed must differ from the public development seed")
    return _write_splits(
        root,
        seed,
        [Split.VALIDATION, Split.HIDDEN_TRANSFER],
        f"{DATASETS[dataset].version}-private" + (f"-x{scale}" if scale > 1 else ""),
        record_seed=False,
        dataset=dataset,
        scale=scale,
    )


class DatasetLoader:
    def __init__(
        self,
        root: Path,
        access: EvaluatorAccess | None = None,
        private_root: Path | None = None,
    ):
        self._root = root
        self._access = access
        self._private_root = private_root

    def _root_for(self, split: Split) -> Path:
        if split != Split.DISCOVERY and self._private_root is not None:
            return self._private_root
        return self._root

    def manifest(self, split: Split = Split.DISCOVERY) -> dict[str, object]:
        data: dict[str, object] = json.loads((self._root_for(split) / "manifest.json").read_text())
        return data

    def load(self, split: Split) -> LoadedSplit:
        if split != Split.DISCOVERY and self._access is None:
            raise SplitAccessError(f"{split.value} requires evaluator access")
        root = self._root_for(split)
        manifest = self.manifest(split)
        entry = manifest["splits"][split.value]  # type: ignore[index]
        directory = root / split.value.lower()
        actual: dict[str, str] = {}
        tasks: list[BenchTask] = []
        for path in sorted(directory.glob("*.json")):
            text = path.read_text()
            actual[path.name] = _sha(text)
            tasks.append(BenchTask.model_validate_json(text))
        if actual != entry["files"] or _content_hash(actual) != entry["content_hash"]:
            raise DatasetIntegrityError(f"{split.value} does not match manifest hashes")
        version = DatasetVersion(
            name=str(manifest["dataset"]),
            version=str(manifest["version"]),
            split=split,
            content_hash=str(entry["content_hash"]),
            n_tasks=len(tasks),
        )
        return LoadedSplit(version=version, tasks=tasks)
