"""Stress fixtures are valid DSL, eligible where intended, and the drifted ones really drift."""

import pytest

from experience_engine.adapters.llm.fake import HeuristicDecisionProvider, HeuristicFakeLLM
from experience_engine.bench.dataset import DEFAULT_SEED, generate_split
from experience_engine.bench.environment import SimulatedEnvironment
from experience_engine.bench.verifier import SimulationVerifier
from experience_engine.capabilities.validator import validate_definition
from experience_engine.controller_fixtures import RETIRED_KEY, portfolio
from experience_engine.domain.benchmark import Split
from experience_engine.domain.capability import Literal_, ToolCall, VarRef
from experience_engine.domain.outcome import OutcomeStatus
from experience_engine.execution.capability_runtime import (
    CapabilityRuntime,
    StaticCapabilityCatalog,
)
from experience_engine.execution.controller import RuleCapabilityPolicy
from experience_engine.execution.matcher import CapabilityMatcher
from experience_engine.execution.orchestrator import TaskOrchestrator
from support import FakeClock, config


def orchestrate(defs, task):  # type: ignore[no-untyped-def]
    catalog = StaticCapabilityCatalog("bench", defs)
    runtime = CapabilityRuntime(
        catalog=catalog,
        matcher=CapabilityMatcher(catalog),
        decisions_for=lambda _llm: HeuristicDecisionProvider(),
        experimental=True,
    )
    orch = TaskOrchestrator(
        controller=RuleCapabilityPolicy(),
        verifier=SimulationVerifier(),
        clock=FakeClock(),
        config=config(),
        capabilities=runtime,
    )
    return orch.execute(
        task.task,
        SimulatedEnvironment(task.environment, task.task.allowed_tools),
        HeuristicFakeLLM(),
    )


def key_expr(defn):  # type: ignore[no-untyped-def]
    apply = next(s for s in defn.steps if isinstance(s, ToolCall) and s.id == "apply_fix")
    return apply.args["key"]


def test_portfolios_are_valid_unique_and_differ_only_in_the_frozen_key() -> None:
    for name in ("good", "heterogeneous", "drifted"):
        defs = portfolio(name)
        assert len({d.id for d in defs}) == 2
        for d in defs:
            validate_definition(d)
    good, hetero, drifted = portfolio("good"), portfolio("heterogeneous"), portfolio("drifted")
    assert all(isinstance(key_expr(d), VarRef) for d in good)
    assert isinstance(key_expr(hetero[0]), VarRef) and isinstance(key_expr(hetero[1]), Literal_)
    assert all(isinstance(key_expr(d), Literal_) for d in drifted)
    assert key_expr(drifted[0]).literal == RETIRED_KEY["pool"]
    with pytest.raises(ValueError):
        portfolio("unknown")


def test_a_drifted_capability_fails_on_every_layout_and_hands_over_to_the_planner() -> None:
    pools = {
        b.variant: b for b in generate_split(Split.DISCOVERY, DEFAULT_SEED) if b.family == "pool"
    }
    (pool_drifted,) = [d for d in portfolio("drifted") if d.id.startswith("pool.")]
    (pool_good,) = [d for d in portfolio("good") if d.id.startswith("pool.")]
    for variant, bench in pools.items():
        good = orchestrate([pool_good], bench)
        assert good.outcome.status == OutcomeStatus.VERIFIED_SUCCESS, variant
        assert good.runtime.fallback_reason is None
        stale = orchestrate([pool_drifted], bench)
        assert stale.runtime.fallback_reason and "TOOL_FAILED" in stale.runtime.fallback_reason
        assert stale.outcome.capability_version == pool_drifted.id + "@1"
        # the retired key changes nothing in the environment: the planner starts from a clean state
        assert stale.runtime.tool_calls > good.runtime.tool_calls or stale.outcome.tokens_used > 0
