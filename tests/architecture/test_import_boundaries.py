"""Pure packages must not depend on frameworks/adapters; layering must stay one-directional."""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "experience_engine"
FORBIDDEN = {
    "fastapi",
    "celery",
    "sqlalchemy",
    "alembic",
    "langgraph",
    "langchain",
    "openai",
    "anthropic",
    "httpx",
    "requests",
    "psycopg",
    "pika",
    "kombu",
    "urllib",
    "socket",
    "subprocess",
    "sqlite3",
}
PURE = ["domain", "ports", "execution", "learning", "capabilities", "bench"]
ALLOWED_INTERNAL = {
    "domain": {"domain"},
    "ports": {"domain", "ports"},
    "execution": {"domain", "ports", "execution"},
    "learning": {"domain", "ports", "learning", "capabilities"},
    "capabilities": {"domain", "capabilities"},
    "bench": {"domain", "ports", "execution", "bench"},
}


def _imports(path: Path) -> list[str]:
    names: list[str] = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.append(node.module)
    return names


@pytest.mark.parametrize("package", PURE)
def test_pure_packages_have_no_framework_or_io_imports(package: str) -> None:
    for path in (SRC / package).rglob("*.py"):
        for name in _imports(path):
            assert name.split(".")[0] not in FORBIDDEN, f"{path} imports {name}"


@pytest.mark.parametrize("package", PURE)
def test_layering_is_one_directional(package: str) -> None:
    for path in (SRC / package).rglob("*.py"):
        for name in _imports(path):
            parts = name.split(".")
            if parts[0] == "experience_engine" and len(parts) > 1:
                assert parts[1] in ALLOWED_INTERNAL[package] | {"__init__"} or (len(parts) == 1), (
                    f"{path} imports {name}"
                )


def test_no_dynamic_code_execution_in_source() -> None:
    banned = {"eval", "exec", "compile", "__import__"}
    for path in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in banned, f"{path}: {node.func.id}()"
