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


# ---- private evaluation data (ADR 0004 open item) ---------------------------------------------
def test_private_splits_are_secret_seeded_separate_and_loadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from experience_engine.bench.dataset import write_private_dataset

    private = tmp_path / "private"
    manifest = write_private_dataset(private, seed=987654321)
    assert "seed" not in manifest and manifest["version"].endswith("-private")  # type: ignore[union-attr]
    assert sorted(p.name for p in private.iterdir()) == [
        "hidden_transfer",
        "manifest.json",
        "validation",
    ]

    monkeypatch.setenv("EE_EVALUATOR_ROLE", "evaluator")
    loader = DatasetLoader(COMMITTED, EvaluatorAccess.from_env(), private_root=private)
    hidden = loader.load(Split.HIDDEN_TRANSFER)
    public_hidden = DatasetLoader(COMMITTED, EvaluatorAccess()).load(Split.HIDDEN_TRANSFER)
    assert hidden.version.n_tasks == public_hidden.version.n_tasks
    assert hidden.version.content_hash != public_hidden.version.content_hash  # values differ
    assert hidden.version.version.endswith("-private")
    # discovery always comes from the public root and is unchanged
    assert loader.load(Split.DISCOVERY).version.content_hash == (
        DatasetLoader(COMMITTED).load(Split.DISCOVERY).version.content_hash
    )
    # private data still needs the evaluator capability
    with pytest.raises(SplitAccessError):
        DatasetLoader(COMMITTED, None, private_root=private).load(Split.VALIDATION)


def test_private_seed_must_not_be_the_public_one(tmp_path: Path) -> None:
    from experience_engine.bench.dataset import write_private_dataset

    with pytest.raises(ValueError, match="differ"):
        write_private_dataset(tmp_path, DEFAULT_SEED)


def test_cli_refuses_private_data_inside_the_repository_or_without_seed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from experience_engine.cli import main

    monkeypatch.delenv("EE_PRIVATE_DATASET_SEED", raising=False)
    assert main(["generate", "--private", "--root", str(tmp_path / "p")]) == 2  # no seed
    monkeypatch.setenv("EE_PRIVATE_DATASET_SEED", "424242")
    assert main(["generate", "--private", "--root", str(COMMITTED.parent / "private-tmp")]) == 2
    assert not (COMMITTED.parent / "private-tmp").exists()
    assert main(["generate", "--private", "--root", str(tmp_path / "p")]) == 0
