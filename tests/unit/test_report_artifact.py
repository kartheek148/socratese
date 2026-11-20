import json
from pathlib import Path

import pytest
from test_evaluation_registry import ID, FakeRegistryStore, report

from experience_engine.adapters.artifacts.local_fs import LocalFsArtifactStore
from experience_engine.capabilities.registry import RegistryService
from experience_engine.domain.evaluation import Verdict, default_policies
from experience_engine.ports.artifacts import ArtifactNotFoundError
from support import FakeClock


def test_full_report_is_stored_as_a_content_addressed_artifact(tmp_path: Path) -> None:
    store = FakeRegistryStore()
    artifacts = LocalFsArtifactStore(tmp_path)
    svc = RegistryService(store, default_policies(), FakeClock(), artifacts)  # type: ignore[arg-type]
    svc.start_validation("bench", ID, 1, "ev")
    svc.complete_evaluation(report(Verdict.PASS), "ev")
    (_rep, sha) = store.evaluations[0]
    assert sha == store.artifact_refs[0].sha256  # type: ignore[attr-defined]
    stored = json.loads(artifacts.get("bench", sha))
    assert stored["capability_id"] == ID and stored["verdict"] == "PASS"
    with pytest.raises(ArtifactNotFoundError):  # another tenant cannot read it
        artifacts.get("other", sha)


def test_without_an_artifact_store_the_hash_is_still_recorded() -> None:
    store = FakeRegistryStore()
    svc = RegistryService(store, default_policies(), FakeClock())  # type: ignore[arg-type]
    svc.start_validation("bench", ID, 1, "ev")
    svc.complete_evaluation(report(Verdict.FAIL), "ev")
    assert len(store.evaluations[0][1]) == 64 and store.artifact_refs == []
