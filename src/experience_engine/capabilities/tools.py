"""Approved tool contracts for capabilities. A capability may only call tools listed here, with
exactly these argument names. Milestone 4's executor binds these names to the real tool gateway.
"""

from dataclasses import dataclass

from experience_engine.domain.capability import PathRole


@dataclass(frozen=True)
class ToolContract:
    name: str
    args: frozenset[str]
    mutating: bool
    # arguments that must resolve to a file path
    path_args: frozenset[str] = frozenset()


APPROVED_TOOLS: dict[str, ToolContract] = {
    "list_files": ToolContract("list_files", frozenset(), mutating=False),
    "read_file": ToolContract(
        "read_file", frozenset({"path"}), mutating=False, path_args=frozenset({"path"})
    ),
    "set_json_value": ToolContract(
        "set_json_value",
        frozenset({"path", "key", "value"}),
        mutating=True,
        path_args=frozenset({"path"}),
    ),
}

# Prompt templates an LLM_DECISION may reference. Templates live in code, not in definitions.
APPROVED_TEMPLATES = frozenset({"select_fix_parameters/1"})
APPROVED_VERIFIERS = frozenset({"outcome_verifier"})


def classify_path(path: str) -> PathRole:
    """Deterministic file-role classification shared by the compiler and (M4) the executor."""
    top = path.strip().lstrip("/").split("/", 1)[0].lower()
    if top in {"logs", "log", "var"}:
        return PathRole.LOG
    if top in {"infra", "infrastructure"}:
        return PathRole.INFRA
    return PathRole.CONFIG
