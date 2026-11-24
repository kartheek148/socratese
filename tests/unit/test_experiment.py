"""Capability-vs-memory experiment: memory builders, retrieval, paired statistics, lifecycle
economics, the paired grid runner, arm runners and the report."""

import json
import math
from typing import Any

import pytest
from test_capability_runtime import MemoryCatalog, make
from test_learning import STATUS, traj

from experience_engine.adapters.llm.fake import HeuristicDecisionProvider, HeuristicFakeLLM
from experience_engine.bench.dataset import DEFAULT_SEED, generate_split
from experience_engine.bench.verifier import SimulationVerifier
from experience_engine.domain.benchmark import AgentConfig, ModelSettings, Split
from experience_engine.domain.experiment import (
    ExperimentConfig,
    ExperimentResult,
    LearningLedger,
    RunRow,
)
from experience_engine.evaluation import economics, paired
from experience_engine.evaluation.arms import ArmOutcome
from experience_engine.evaluation.experiment import (
    ExperimentTask,
    applicability_matrix,
    run_experiment,
    tag_tasks,
    transfer_level,
)
from experience_engine.evaluation.experiment_arms import (
    make_baseline_arm,
    make_memory_arm,
    make_runtime_arm,
    task_query,
)
from experience_engine.evaluation.experiment_report import render_experiment
from experience_engine.execution.capability_runtime import (
    CapabilityRuntime,
    StaticCapabilityCatalog,
)
from experience_engine.execution.matcher import CapabilityMatcher
from experience_engine.memory.bank import ExperienceMemory, TaskQuery
from experience_engine.memory.builders import (
    PREAMBLE,
    consolidate_family,
    estimate_tokens,
    render_instructions,
)
from experience_engine.memory.inject import MemoryAugmentedLLM
from support import FakeClock, ScriptedLLM, first_task

FULL = (
    "list_files",
    "read_file:config",
    "read_file:infra",
    "read_file:log",
    "set_json_value:config",
)
NO_LOG = ("list_files", "read_file:config", "read_file:infra", "set_json_value:config")
Q = TaskQuery("pool", frozenset({"config", "infra", "log"}), frozenset({"list_files"}))


def experience() -> list:  # type: ignore[type-arg]
    ok = [traj(tokens=FULL) for _ in range(4)]
    bad = [traj(tokens=NO_LOG, status=STATUS.FAILURE) for _ in range(5)]
    other = [traj(family="timeout", tokens=FULL) for _ in range(3)]
    return ok + bad + other


# ---- memory ---------------------------------------------------------------------------------
def test_episodic_memory_has_outcomes_and_a_failure_and_prefers_the_matching_family() -> None:
    got = ExperienceMemory(experience()).episodic(Q)
    assert got.items == 3 and got.text.startswith(PREAMBLE)
    assert got.text.count("VERIFIED SUCCESS") == 2 and got.text.count("FAILED independent") == 1
    assert "task family timeout" not in got.text  # matching family ranks first
    assert "read_file(log)" in got.text and got.retrieved_tokens == estimate_tokens(got.text)


def test_consolidated_memory_states_applicability_and_the_discriminative_failure_pattern() -> None:
    text = consolidate_family("pool", [t for t in experience() if t.task_family == "pool"])
    assert "4 independently verified successes and 5 failures" in text
    assert "Applies when" in text and "roles" in text
    assert "read_file(log) was done in 100% of successes but only 0% of failures" in text
    assert "5 of 5 failed runs claimed success" in text


def test_instructions_render_every_step_of_the_compiled_procedure() -> None:
    text = render_instructions(make())
    for needle in (
        "1. List the files",
        "Read the application configuration file",
        "Decide the single",
        "set_json_value",
        "Check the result",
        "Applies when",
        "Limitation:",
    ):
        assert needle in text


def test_unseen_family_gets_all_experience_for_every_memory_form_and_none_without_any() -> None:
    mem = ExperienceMemory(experience(), [make()])
    q = TaskQuery("queue", Q.roles, Q.tools)
    assert "pool" in mem.consolidated(q).text and "timeout" in mem.consolidated(q).text
    assert mem.instructions(q).items == 1 and mem.episodic(q).items == 3
    empty = ExperienceMemory([])
    assert empty.episodic(Q).text == "" and empty.consolidated(Q).items == 0


def test_retrieval_is_deterministic_and_order_independent() -> None:
    a, b = experience(), list(reversed(experience()))
    assert ExperienceMemory(a).episodic(Q).text == ExperienceMemory(b).episodic(Q).text
    assert ExperienceMemory(a).consolidated(Q).text == ExperienceMemory(b).consolidated(Q).text


def test_memory_is_built_from_trajectories_only() -> None:
    # raw values and paths cannot reach memory: a Trajectory has none (information parity)
    text = ExperienceMemory(experience(), [make()]).episodic(Q).text
    assert "app/config.json" not in text and "pool_size" not in text


def test_injection_appends_to_the_system_prompt_and_records_context_size() -> None:
    from experience_engine.ports.llm import ChatMessage, LLMRequest

    class Echo:
        seen: list[str] = []

        def complete(self, request: LLMRequest):  # type: ignore[no-untyped-def]
            from experience_engine.ports.llm import LLMResponse

            Echo.seen.append(request.messages[0].content)
            return LLMResponse(text="{}", prompt_tokens=1, completion_tokens=1)

    model = ModelSettings(provider="fake", model="m")
    req = LLMRequest(
        messages=[ChatMessage(role="system", content="SYS"), ChatMessage(role="user", content="u")],
        model=model,
    )
    wrapped = MemoryAugmentedLLM(Echo(), "MEMORY")
    wrapped.complete(req)
    assert Echo.seen[-1] == "SYS\n\nMEMORY" and wrapped.first_prompt_tokens > 0
    MemoryAugmentedLLM(Echo(), "").complete(req)
    assert Echo.seen[-1] == "SYS"


# ---- paired statistics ----------------------------------------------------------------------
def row(
    arm: str,
    task: str,
    rep: int,
    ok: bool,
    *,
    status: str | None = None,
    tokens: int = 100,
    **kw: Any,
) -> RunRow:
    return RunRow(
        arm=arm, task_id=task, rep=rep, split="VALIDATION", family=kw.pop("family", "pool"),
        variant=kw.pop("variant", "v"), level=kw.pop("level", "intra_variant"),
        status=status or ("VERIFIED_SUCCESS" if ok else "FAILURE"), verified=ok, tokens=tokens,
        tool_calls=3, latency_ms=10, **kw,
    )  # fmt: skip


def grid(outcomes: dict[str, list[list[bool]]]) -> list[RunRow]:
    """outcomes[arm][task_index] = per-repetition results."""
    return [
        row(arm, f"t{i}", rep, ok)
        for arm, tasks in outcomes.items()
        for i, reps in enumerate(tasks)
        for rep, ok in enumerate(reps)
    ]


def test_mcnemar_exact_matches_known_values() -> None:
    assert paired.mcnemar_exact(0, 0) == 1.0 and paired.mcnemar_exact(4, 4) == 1.0
    assert paired.mcnemar_exact(5, 0) == pytest.approx(0.0625)
    assert paired.mcnemar_exact(0, 10) == pytest.approx(2 * 1 / 1024)
    assert paired.mcnemar_exact(2, 8) == pytest.approx(0.109375)


def test_holm_is_monotone_capped_and_controls_the_family() -> None:
    adj = paired.holm({"a": 0.01, "b": 0.04, "c": 0.03})
    assert adj == {"a": pytest.approx(0.03), "c": pytest.approx(0.06), "b": pytest.approx(0.06)}
    assert paired.holm({"x": 0.9})["x"] == 0.9 and paired.holm({"x": 0.6, "y": 0.7})["y"] == 1.0


def test_bootstrap_is_reproducible_and_brackets_the_mean() -> None:
    values = [1.0, 1.0, 0.5, 0.0, 1.0, 0.5, 0.0, 1.0]
    mean, lo, hi = paired.bootstrap_mean_ci(values)
    assert (mean, lo, hi) == paired.bootstrap_mean_ci(values)
    assert lo <= mean <= hi and lo < hi
    assert paired.bootstrap_mean_ci([0.25] * 5) == (0.25, 0.25, 0.25)
    assert paired.bootstrap_mean_ci([]) == (0.0, 0.0, 0.0)


def test_repetitions_do_not_inflate_the_number_of_tasks() -> None:
    rows = grid({"X": [[True] * 10] * 3, "Y": [[False] * 10] * 3})  # 3 tasks x 10 reps
    c = paired.compare(rows, "X", "Y")
    assert c.n_tasks == 3 and (c.only_x, c.only_y) == (3, 0)
    # with only 3 tasks the exact test cannot reach significance however many repetitions exist
    assert c.p_mcnemar == pytest.approx(0.25)


def test_unknown_runs_are_missing_data_not_failures() -> None:
    rows = grid({"X": [[True, True], [True, True]], "Y": [[False, False], [False, False]]})
    rows += [row("X", "t2", 0, False, status="UNKNOWN"), row("Y", "t2", 0, False)]
    c = paired.compare(rows, "X", "Y")
    assert c.n_tasks == 2 and c.n_excluded == 1  # t2 has no known X run
    assert paired.task_rates(rows, "X").keys() == {"t0", "t1"}


def test_solved_threshold_is_at_least_half_of_the_repetitions() -> None:
    rows = grid({"X": [[True, False]], "Y": [[False, False]]})
    c = paired.compare(rows, "X", "Y")
    assert c.rate_x == 0.5 and (c.only_x, c.only_y) == (1, 0)


def test_a_task_solved_in_fewer_than_half_its_repetitions_is_not_solved() -> None:
    rows = grid({"X": [[True, False, False]], "Y": [[False, False, False]]})
    c = paired.compare(rows, "X", "Y")
    assert c.rate_x == pytest.approx(1 / 3) and (c.only_x, c.only_y, c.neither) == (0, 0, 1)


def test_discordant_counts_and_where_filter() -> None:
    rows = grid({"X": [[True], [True], [False], [True]], "Y": [[False], [True], [True], [False]]})
    c = paired.compare(rows, "X", "Y")
    assert (c.only_x, c.only_y, c.both, c.neither) == (2, 1, 1, 0) and c.n_tasks == 4
    sub = paired.compare(rows, "X", "Y", where=lambda r: r.task_id in {"t0", "t1"})
    assert sub.n_tasks == 2 and sub.only_x == 1


def test_success_at_a_token_budget_charges_expensive_successes_as_failures() -> None:
    rows = [row("A", "t0", 0, True, tokens=5000), row("A", "t1", 0, True, tokens=500)]
    assert paired.success_at_budget(rows, "A", 1000) == 0.5
    assert paired.budget_curve(rows, "A", [100, 10_000]) == {100: 0.0, 10_000: 1.0}


# ---- lifecycle economics --------------------------------------------------------------------
LEDGER = LearningLedger(
    discovery_tokens=10_000, compilation_tokens=0, validation_tokens=6_000, consolidation_tokens=0,
    wall_ms_compilation=50,
)  # fmt: skip


def econ_rows() -> list[RunRow]:
    rows = [row("A", f"t{i}", 0, i < 2, tokens=1000) for i in range(10)]  # 20% at 1000/run
    rows += [row("E", f"t{i}", 0, i < 8, tokens=200) for i in range(10)]  # 80% at 200/run
    rows += [row("B", f"t{i}", 0, i < 2, tokens=1100) for i in range(10)]  # 20% at 1100/run
    return rows


def test_break_even_uses_tokens_per_verified_success_and_counts_validation_only_for_e() -> None:
    table = economics.lifecycle_table(econ_rows(), LEDGER, arms=["A", "B", "E"])
    assert table["A"].tokens_per_success == pytest.approx(5000)
    assert table["E"].tokens_per_success == pytest.approx(250)
    assert table["E"].learning_tokens == 16_000  # discovery + validation
    assert table["E"].breakeven_successes == pytest.approx(16_000 / (5000 - 250))
    assert table["E"].lifecycle_cost(100) == 16_000 + 100 * 200
    assert table["B"].learning_tokens == 10_000  # not validated as built
    assert table["B"].breakeven_successes is None and "never" in table["B"].breakeven_note
    assert table["A"].learning_tokens == 0 and table["A"].breakeven_note == "reference"


def test_sensitivity_charges_validation_to_every_learning_arm() -> None:
    table = economics.lifecycle_table(
        econ_rows(), LEDGER, arms=["A", "B", "E"], charge_validation_to_all=True
    )
    assert table["B"].learning_tokens == 16_000 and table["A"].learning_tokens == 0


def test_maintenance_cost_delays_break_even() -> None:
    plain = economics.lifecycle_table(econ_rows(), LEDGER, arms=["A", "E"])["E"]
    maint = economics.lifecycle_table(
        econ_rows(), LEDGER, arms=["A", "E"], maintenance_tokens=4750
    )["E"]
    assert maint.breakeven_successes == pytest.approx(plain.breakeven_successes + 1)  # type: ignore[operator]
    assert plain.breakeven_tasks == pytest.approx(16_000 / (1000 - 200))


def test_baseline_with_no_successes_and_arm_with_none() -> None:
    rows = [row("A", "t0", 0, False), row("E", "t0", 0, True), row("B", "t0", 0, False)]
    t = economics.lifecycle_table(rows, LEDGER, arms=["A", "B", "E"])
    assert t["E"].breakeven_successes == 0.0 and "no verified" in t["E"].breakeven_note
    assert t["B"].breakeven_successes is None


# ---- grid runner ----------------------------------------------------------------------------
def tasks_for(n: int = 4) -> list[ExperimentTask]:
    bench = generate_split(Split.VALIDATION, DEFAULT_SEED)[:n]
    return tag_tasks("VALIDATION", bench, {"pool", "timeout"})


def ok_runner(verified: bool, tokens: int = 10):  # type: ignore[no-untyped-def]
    def run(b):  # type: ignore[no-untyped-def]
        return ArmOutcome("VERIFIED_SUCCESS" if verified else "FAILURE", verified, tokens, 1, 1)

    return run


def test_every_arm_runs_on_identical_tasks_and_repetitions() -> None:
    rows = run_experiment(
        {"A": ok_runner(False), "B": ok_runner(True)}, tasks_for(4), repetitions=3
    )
    assert len(rows) == 4 * 3 * 2
    for arm in "AB":
        assert {(r.task_id, r.rep) for r in rows if r.arm == arm} == {
            (r.task_id, r.rep) for r in rows if r.arm != arm
        }
    assert all(r.level == "intra_variant" and r.split == "VALIDATION" for r in rows)


def test_concurrent_and_serial_grids_are_identical_and_crashes_become_unknown() -> None:
    def crashing(b):  # type: ignore[no-untyped-def]
        if b.task.task_id.endswith("-01"):
            raise RuntimeError("provider exploded")
        return ArmOutcome("VERIFIED_SUCCESS", True, 5, 1, 1)

    arms = {"A": crashing, "B": ok_runner(True)}
    serial = run_experiment(arms, tasks_for(), repetitions=2, workers=1)
    parallel = run_experiment(arms, tasks_for(), repetitions=2, workers=6)
    assert [r.model_dump() for r in serial] == [r.model_dump() for r in parallel]
    crashed = [r for r in serial if r.status == "UNKNOWN"]
    assert crashed and all(
        r.termination == "ERROR:RuntimeError" and not r.verified for r in crashed
    )
    assert paired.compare(serial, "A", "B").n_excluded >= 1  # excluded, not scored as failures


def test_transfer_levels_come_from_what_discovery_contained() -> None:
    assert transfer_level("DISCOVERY", "pool", {"pool"}) == "source"
    assert transfer_level("VALIDATION", "pool", {"pool"}) == "intra_variant"
    assert transfer_level("HIDDEN_TRANSFER", "pool", {"pool"}) == "cross_variant"
    assert transfer_level("HIDDEN_TRANSFER", "queue", {"pool", "timeout"}) == "cross_family"


def test_applicability_gate_admits_only_the_learned_family_and_refuses_violations() -> None:
    hidden = tag_tasks("HIDDEN_TRANSFER", generate_split(Split.HIDDEN_TRANSFER, 3, "v3"), {"pool"})
    (entry,) = applicability_matrix([make()], hidden)
    by = entry["admitted_by_family"]
    assert by["pool"] == [6, 6] and by["queue"] == [0, 6] and by["timeout"] == [0, 6]  # type: ignore[index]
    assert entry["synthetic_violations_admitted"][0] == 0  # type: ignore[index]


# ---- arms -----------------------------------------------------------------------------------
def cfg(config_id: str) -> AgentConfig:
    return AgentConfig(
        config_id=config_id, prompt_version="t", model=ModelSettings(provider="fake", model="m")
    )


def test_memory_arm_injects_retrieved_text_and_reports_its_cost() -> None:
    bench = first_task()
    mem = ExperienceMemory(experience())
    seen: list[str] = []

    class Spy(HeuristicFakeLLM):
        def complete(self, request):  # type: ignore[no-untyped-def]
            seen.append(request.messages[0].content)
            return super().complete(request)

    arm = make_memory_arm(
        llm=Spy(), config=cfg("B"), verifier=SimulationVerifier(), clock=FakeClock(),
        retrieve=mem.episodic,
    )  # fmt: skip
    out = arm(bench)
    assert out.verified and all("Past episodes:" in s for s in seen)
    assert out.retrieved_tokens > 0 and out.context_tokens > out.retrieved_tokens
    assert out.memory_sha256 and len(out.memory_sha256) == 64
    assert task_query(bench).family == "pool" and "log" in task_query(bench).roles


def test_baseline_arm_receives_nothing_but_records_the_same_fields() -> None:
    out = make_baseline_arm(
        llm=HeuristicFakeLLM(), config=cfg("A"), verifier=SimulationVerifier(), clock=FakeClock()
    )(first_task())
    assert out.verified and out.retrieved_tokens == 0 and out.memory_sha256 is None
    assert out.context_tokens > 0


def runtime(definitions: list, decisions: Any = None) -> CapabilityRuntime:  # type: ignore[type-arg]
    catalog = StaticCapabilityCatalog("bench", definitions)
    return CapabilityRuntime(
        catalog=catalog, matcher=CapabilityMatcher(catalog),
        decisions_for=lambda _llm: decisions or HeuristicDecisionProvider(), experimental=True,
    )  # fmt: skip


def test_runtime_arm_serves_from_the_capability_and_reports_applicability() -> None:
    arm = make_runtime_arm(
        llm=ScriptedLLM([]), config=cfg("E"), verifier=SimulationVerifier(), clock=FakeClock(),
        runtime=runtime([make()]),
    )  # fmt: skip
    out = arm(first_task())
    assert (out.verified, out.applicable, out.capability_used, out.fell_back) == (
        True,
        True,
        True,
        False,
    )
    other = next(t for t in generate_split(Split.DISCOVERY, DEFAULT_SEED) if t.family == "timeout")
    out2 = make_runtime_arm(
        llm=HeuristicFakeLLM(), config=cfg("E"), verifier=SimulationVerifier(), clock=FakeClock(),
        runtime=runtime([make()]),
    )(other)  # fmt: skip
    assert (out2.applicable, out2.capability_used) == (False, False)  # falls through to planning


def test_runtime_arm_reports_fallback_to_general_planning() -> None:
    from experience_engine.ports.decisions import DecisionError

    class Failing:
        def decide(self, **_: Any) -> Any:
            raise DecisionError("model unavailable")

    out = make_runtime_arm(
        llm=HeuristicFakeLLM(), config=cfg("E"), verifier=SimulationVerifier(), clock=FakeClock(),
        runtime=runtime([make()], Failing()),
    )(first_task())  # fmt: skip
    assert (out.applicable, out.capability_used, out.fell_back) == (True, True, True)
    assert out.verified  # general planning finished the job within the remaining budget


def test_static_catalog_is_tenant_scoped_and_deterministic() -> None:
    cat = StaticCapabilityCatalog("bench", [make(id="pool.00000000bb"), make(id="pool.00000000aa")])
    assert [d.id for d in cat.list_executable("bench")] == ["pool.00000000aa", "pool.00000000bb"]
    assert (
        cat.list_executable("other") == []
        and cat.get_executable("other", "pool.00000000aa", 1) is None
    )
    assert cat.get_executable("bench", "pool.00000000aa", 1) is not None
    assert MemoryCatalog([make()]).list_executable("bench")  # sanity: test double unaffected


# ---- report ---------------------------------------------------------------------------------
def synthetic_result() -> ExperimentResult:
    rows: list[RunRow] = []
    levels = [("intra_variant", "pool"), ("cross_variant", "pool"), ("cross_family", "queue")]
    for i in range(12):
        level, fam = levels[i % 3]
        for rep in range(2):
            for arm, p in {
                "A": 0.25,
                "B": 0.5,
                "C": 0.5,
                "D": 0.6,
                "E": 0.9 if fam == "pool" else 0.2,
            }.items():
                ok = ((i * 7 + rep * 3 + ord(arm)) % 10) / 10 < p
                rows.append(row(
                    arm, f"t{i}", rep, ok, tokens=300 if arm == "E" else 900, family=fam,
                    level=level, variant="x", retrieved_tokens=0 if arm in "AE" else 200,
                    context_tokens=1000, applicable=(fam == "pool") if arm == "E" else None,
                    capability_used=(fam == "pool") if arm == "E" else None,
                    fell_back=False if arm == "E" else None, termination="FINAL",
                ))  # fmt: skip
    cfg_ = ExperimentConfig(
        name="t", model={"provider": "fake", "model": "m", "temperature": 0.0},
        arms=list("ABCDE"), repetitions=2, workers=1,
        datasets={"VALIDATION": {"name": "d", "version": "1", "content_hash": "a" * 64}},
        discovery_trajectories=10, discovery_families=["pool"], capabilities=["pool.x@1"],
        capability_catalog_states=["CANDIDATE"], memory_preamble_sha256="b" * 64, max_episodes=3,
        verifier_version="v", runtime_version="r", evaluator_version="e",
    )  # fmt: skip
    ledger = LearningLedger(discovery_tokens=20_000, validation_tokens=8_000)
    return ExperimentResult(
        config=cfg_, ledger=ledger, ledger_by_family={"pool": ledger}, rows=rows,
        applicability=[{"capability": "pool.x@1", "family": "pool",
                        "admitted_by_family": {"pool": [8, 8], "queue": [0, 4]},
                        "synthetic_violations_admitted": [0, 4]}],
    )  # fmt: skip


def test_report_contains_every_required_section_and_computed_numbers() -> None:
    result = synthetic_result()
    text = render_experiment(result, title="T")
    for section in (
        "## 1. Configuration", "## 2. Success by arm", "By transfer level", "By variant",
        "### 3.1 Primary", "### 3.2 Attribution ladder", "## 4. Execution efficiency",
        "## 5. Lifecycle cost and break-even", "### 5.2 Sensitivity", "### 5.3 Family `pool`",
        "## 6. Compiled capability", "## 7. Failure and regression analysis",
        "## 8. Mechanical verdicts", "## 9. Statistical assumptions",
    ):  # fmt: skip
        assert section in text, section
    assert "Experimental catalog" in text or "EXPERIMENTAL" in text.upper()
    assert "E vs A" in text and "Holm-adj p" in text and "McNemar" in text
    expected = paired.compare(result.rows, "E", "A")
    assert f"{expected.diff:+.0%}" in text
    assert "Maintenance M = 0 tokens (**not measured**" in text
    assert render_experiment(result, title="T") == text  # reproducible


def test_results_round_trip_through_json() -> None:
    result = synthetic_result()
    again = ExperimentResult.model_validate_json(result.model_dump_json())
    assert again == result and json.loads(result.model_dump_json())["config"]["repetitions"] == 2


def test_report_never_hardcodes_a_winner() -> None:
    # make memory strictly better than the capability: the mechanical verdict must flip
    result = synthetic_result()
    flipped = [
        r.model_copy(
            update={
                "verified": r.arm != "E",
                "status": "FAILURE" if r.arm == "E" else "VERIFIED_SUCCESS",
            }
        )
        for r in result.rows
    ]  # fmt: skip
    text = render_experiment(result.model_copy(update={"rows": flipped}), title="T")
    assert "reading: **B better" in text or "reading: **C better" in text


def test_a_winner_requires_bootstrap_and_mcnemar_to_agree() -> None:
    from experience_engine.evaluation.experiment_report import _verdict

    def comp(diff: float, lo: float, hi: float, p: float) -> paired.PairedComparison:
        return paired.PairedComparison("X", "Y", 10, 0, 0.5, 0.4, diff, lo, hi, 3, 0, 5, 2, p)

    assert _verdict(comp(0.2, 0.1, 0.3, 0.01)) == "X better (bootstrap and McNemar)"
    assert "bootstrap only" in _verdict(comp(0.2, 0.1, 0.3, 0.3))
    assert "McNemar only" in _verdict(comp(0.2, -0.1, 0.3, 0.01))
    assert _verdict(comp(0.2, -0.1, 0.3, 0.3)) == "not distinguishable"
    assert _verdict(comp(-0.2, -0.3, -0.1, 0.01)).startswith("Y better")
    assert "Holm" not in _verdict(comp(0.2, 0.1, 0.3, 0.01), 0.2) and "bootstrap only" in _verdict(
        comp(0.2, 0.1, 0.3, 0.01), 0.2
    )  # the adjusted p-value, when given, is the one used


def test_report_has_per_family_paired_tables() -> None:
    text = render_experiment(synthetic_result(), title="T")
    assert "### 3.4 E vs alternatives, family `pool` only" in text
    assert "### 3.4 E vs alternatives, family `queue` only" in text


def test_estimator_is_ceil_of_chars_over_four() -> None:
    assert estimate_tokens("") == 0 and estimate_tokens("abcde") == math.ceil(5 / 4)
