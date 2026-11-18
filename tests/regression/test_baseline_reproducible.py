"""Regression tests pinning baseline behaviour and reproducibility (fresh-session, no learning)."""

import random
from pathlib import Path

import pytest

from experience_engine.adapters.clock import SystemClock
from experience_engine.adapters.llm.fake import HeuristicFakeLLM
from experience_engine.adapters.results import InMemoryResultStore
from experience_engine.bench.dataset import DatasetLoader, EvaluatorAccess
from experience_engine.bench.harness import run_benchmark
from experience_engine.bench.verifier import SimulationVerifier
from experience_engine.domain.benchmark import BenchmarkRunRecord, Split
from experience_engine.domain.outcome import OutcomeStatus
from experience_engine.execution.controller import FixedPolicy
from support import config

ROOT = Path(__file__).resolve().parents[2] / "benchmarks" / "v1"


def _run(split: Split, reverse: bool = False) -> list[BenchmarkRunRecord]:
    loaded = DatasetLoader(ROOT, EvaluatorAccess()).load(split)
    tasks = list(reversed(loaded.tasks)) if reverse else loaded.tasks
    return run_benchmark(
        tasks,
        dataset=loaded.version,
        seed=0,
        config=config(),
        llm_factory=lambda _t, _s: HeuristicFakeLLM(),
        controller=FixedPolicy(),
        verifier=SimulationVerifier(),
        clock=SystemClock(),
        store=InMemoryResultStore(),
    )


@pytest.mark.parametrize("split", list(Split))
def test_rerun_produces_identical_digests(split: Split) -> None:
    a, b = _run(split), _run(split)
    assert [r.digest() for r in a] == [r.digest() for r in b]
    assert len({r.run_id for r in a + b}) == 2 * len(a)


def test_fresh_session_results_are_order_independent() -> None:
    forward = {r.tags["task_id"]: r.digest() for r in _run(Split.VALIDATION)}
    backward = {r.tags["task_id"]: r.digest() for r in _run(Split.VALIDATION, reverse=True)}
    assert forward == backward


def test_pinned_baseline_outcomes() -> None:
    for split in (Split.DISCOVERY, Split.VALIDATION):
        assert {r.outcome.status for r in _run(split)} == {OutcomeStatus.VERIFIED_SUCCESS}
    hidden = _run(Split.HIDDEN_TRANSFER)
    # The scripted agent claims success on unfamiliar layouts; the verifier must reject every one.
    assert all(r.agent_claimed_done for r in hidden)
    assert {r.outcome.status for r in hidden} == {OutcomeStatus.FAILURE}


def test_records_capture_required_metrics() -> None:
    r = _run(Split.DISCOVERY)[0]
    o = r.outcome
    assert o.tokens_used > 0 and o.tool_calls > 0 and o.execution_time_ms >= 0
    assert o.human_interventions == 0 and o.verifier_version and r.fresh_session
    assert r.dataset.content_hash and r.config.model.model and r.runtime_version
    assert r.started_at.tzinfo is not None and r.decision.outcome_status == o.status
    assert all(s.summary and len(s.summary) <= 200 for s in r.steps)


def test_random_global_state_does_not_leak_into_results() -> None:
    random.seed(1)
    a = [r.digest() for r in _run(Split.DISCOVERY)]
    random.seed(999)
    assert a == [r.digest() for r in _run(Split.DISCOVERY)]
