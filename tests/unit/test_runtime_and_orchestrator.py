from experience_engine.bench.verifier import SimulationVerifier
from experience_engine.domain.outcome import OutcomeStatus, TerminationReason
from experience_engine.ports.llm import LLMError
from support import FakeClock, ScriptedLLM, correct_pool_fix, first_task, orchestrator, session


def _run(task, replies, clock=None, tick=0, verifier=None):  # type: ignore[no-untyped-def]
    clock = clock or FakeClock()
    llm = ScriptedLLM(replies, clock, tick)
    return orchestrator(clock, verifier or SimulationVerifier()).execute(
        task.task, session(task), llm
    ), llm


def test_correct_run_is_verified_and_decision_logged() -> None:
    task = first_task()
    result, _ = _run(task, correct_pool_fix(task))
    assert result.outcome.status == OutcomeStatus.VERIFIED_SUCCESS
    assert result.outcome.tool_calls == 1 and result.outcome.tokens_used == 30
    assert result.runtime.termination == TerminationReason.FINAL
    d = result.decision
    assert d.eligible_actions == [d.selected_action] and d.selection_probability == 1.0
    assert d.policy_version == result.outcome.policy_version
    assert d.outcome_status == OutcomeStatus.VERIFIED_SUCCESS and d.run_id == result.run_id


def test_false_success_claim_is_not_trusted() -> None:
    task = first_task()
    result, _ = _run(task, [{"final": "All fixed, definitely succeeded!"}])
    assert result.runtime.agent_claimed_done
    assert result.outcome.status == OutcomeStatus.FAILURE
    assert result.outcome.failure_reasons


def test_verifier_error_yields_unknown() -> None:
    class Boom:
        version = "boom"

        def verify(self, task, snapshot):  # type: ignore[no-untyped-def]
            raise RuntimeError("down")

    task = first_task()
    result, _ = _run(task, correct_pool_fix(task), verifier=Boom())
    assert result.outcome.status == OutcomeStatus.UNKNOWN


def test_llm_outage_is_unknown_not_failure() -> None:
    result, _ = _run(first_task(), [LLMError("HTTP 503")])
    assert result.runtime.termination == TerminationReason.LLM_ERROR
    assert result.outcome.status == OutcomeStatus.UNKNOWN


def test_disallowed_tool_terminates() -> None:
    result, _ = _run(
        first_task(),
        [{"tool": "write_file", "args": {"path": "app/config.json", "content": "{}"}}],
    )
    assert result.runtime.termination == TerminationReason.TOOL_NOT_ALLOWED
    assert result.outcome.tool_calls == 0
    assert result.outcome.status == OutcomeStatus.FAILURE


def test_step_budget_terminates() -> None:
    task = first_task()
    spec = task.task.model_copy(update={"max_steps": 2})
    clock = FakeClock()
    llm = ScriptedLLM([{"tool": "list_files", "args": {}}] * 5, clock)
    result = orchestrator(clock, SimulationVerifier()).execute(spec, session(task), llm)
    assert result.runtime.termination == TerminationReason.MAX_STEPS and llm.calls == 2


def test_tool_call_budget_terminates() -> None:
    task = first_task()
    spec = task.task.model_copy(update={"max_tool_calls": 1})
    clock = FakeClock()
    llm = ScriptedLLM([{"tool": "list_files", "args": {}}] * 5, clock)
    result = orchestrator(clock, SimulationVerifier()).execute(spec, session(task), llm)
    assert result.runtime.termination == TerminationReason.MAX_TOOL_CALLS
    assert result.outcome.tool_calls == 1


def test_token_budget_terminates_without_executing_over_budget_action() -> None:
    task = first_task()
    spec = task.task.model_copy(update={"max_tokens": 20})
    clock = FakeClock()
    llm = ScriptedLLM([{"tool": "list_files", "args": {}}] * 5, clock)
    result = orchestrator(clock, SimulationVerifier()).execute(spec, session(task), llm)
    assert result.runtime.termination == TerminationReason.MAX_TOKENS
    # 15 tokens/call: call 1 fits and runs; call 2 crosses 20 and its action is not executed.
    assert llm.calls == 2 and result.outcome.tool_calls == 1


def test_deadline_terminates() -> None:
    task = first_task()
    spec = task.task.model_copy(update={"deadline_ms": 100})
    clock = FakeClock()
    llm = ScriptedLLM([{"tool": "list_files", "args": {}}] * 5, clock, tick_ms=150)
    result = orchestrator(clock, SimulationVerifier()).execute(spec, session(task), llm)
    assert result.runtime.termination == TerminationReason.DEADLINE


def test_invalid_replies_recover_once_then_terminate() -> None:
    task = first_task()
    ok, _ = _run(task, ["not json", {"final": "done"}])
    assert ok.runtime.termination == TerminationReason.FINAL
    bad, _ = _run(task, ["nope", "still nope", {"final": "unreached"}])
    assert bad.runtime.termination == TerminationReason.INVALID_ACTION


def test_json_in_code_fence_is_accepted() -> None:
    task = first_task()
    result, _ = _run(task, ['```json\n{"final": "done"}\n```'])
    assert result.runtime.termination == TerminationReason.FINAL


def test_escalation_counts_human_intervention() -> None:
    result, _ = _run(first_task(), [{"escalate": "need a DBA"}])
    assert result.runtime.termination == TerminationReason.ESCALATED
    assert result.outcome.human_interventions == 1
    assert result.outcome.status == OutcomeStatus.FAILURE
