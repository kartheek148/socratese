import json
from pathlib import Path

import pytest

from experience_engine.bench.dataset import (
    DEFAULT_SEED,
    DatasetIntegrityError,
    DatasetLoader,
    EvaluatorAccess,
    SplitAccessError,
    generate_split,
    write_dataset,
)
from experience_engine.domain.benchmark import Split

COMMITTED = Path(__file__).resolve().parents[2] / "benchmarks" / "v1"


def test_generation_is_deterministic_and_seed_sensitive() -> None:
    a = [t.model_dump_json() for t in generate_split(Split.DISCOVERY, DEFAULT_SEED)]
    assert a == [t.model_dump_json() for t in generate_split(Split.DISCOVERY, DEFAULT_SEED)]
    assert a != [t.model_dump_json() for t in generate_split(Split.DISCOVERY, DEFAULT_SEED + 1)]


def test_committed_dataset_matches_generator(tmp_path: Path) -> None:
    write_dataset(tmp_path, DEFAULT_SEED)
    assert (tmp_path / "manifest.json").read_text() == (COMMITTED / "manifest.json").read_text()
    for split in Split:
        for f in sorted((tmp_path / split.value.lower()).glob("*.json")):
            assert f.read_text() == (COMMITTED / split.value.lower() / f.name).read_text()


def test_splits_are_disjoint_and_hidden_uses_unseen_variants() -> None:
    variants = {s: {t.variant for t in generate_split(s)} for s in Split}
    ids = {s: {t.task.task_id for t in generate_split(s)} for s in Split}
    assert not (ids[Split.DISCOVERY] & ids[Split.VALIDATION] & ids[Split.HIDDEN_TRANSFER])
    assert not (variants[Split.HIDDEN_TRANSFER] & variants[Split.DISCOVERY])


def test_hidden_and_validation_require_evaluator_access(monkeypatch: pytest.MonkeyPatch) -> None:
    anonymous = DatasetLoader(COMMITTED)
    assert anonymous.load(Split.DISCOVERY).version.n_tasks == 12
    for split in (Split.VALIDATION, Split.HIDDEN_TRANSFER):
        with pytest.raises(SplitAccessError):
            anonymous.load(split)
    monkeypatch.delenv("EE_EVALUATOR_ROLE", raising=False)
    with pytest.raises(SplitAccessError):
        EvaluatorAccess.from_env()
    monkeypatch.setenv("EE_EVALUATOR_ROLE", "evaluator")
    loader = DatasetLoader(COMMITTED, EvaluatorAccess.from_env())
    assert loader.load(Split.HIDDEN_TRANSFER).version.n_tasks == 8


def test_tampering_is_detected(tmp_path: Path) -> None:
    write_dataset(tmp_path)
    victim = next((tmp_path / "discovery").glob("*.json"))
    doc = json.loads(victim.read_text())
    doc["task"]["objective"] = "tampered"
    victim.write_text(json.dumps(doc))
    with pytest.raises(DatasetIntegrityError):
        DatasetLoader(tmp_path).load(Split.DISCOVERY)


def test_every_task_is_solvable_in_principle() -> None:
    # Reference solutions exist: success criteria are satisfiable within guards.
    for split in Split:
        for t in generate_split(split):
            for crit in t.task.success_criteria:
                if crit.kind == "pool_capacity":
                    assert crit.min_capacity <= crit.max_capacity
                if crit.kind == "timeout_covers_latency":
                    assert max(crit.latency_samples_ms) <= crit.max_timeout_ms
