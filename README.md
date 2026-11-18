# Experience Engine Copilot Blueprint

This package contains a repository-ready starting point for implementing an Experience-Driven Self-Improving Agent.

## Contents

- `.github/copilot-instructions.md` — always-on repository guidance for GitHub Copilot.
- `.github/prompts/implement-milestone.prompt.md` — reusable prompt to implement exactly one milestone at a time.
- `docs/architecture.md` — system architecture, components, interfaces, data model, execution flows, safety, and measurement.
- `docs/implementation-roadmap.md` — milestone plan and acceptance criteria.

## Development (Milestone 1)

```
uv sync
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run pytest
```

Benchmark and baseline: see [docs/benchmark.md](docs/benchmark.md).

## How to use

1. Copy `.github/` and `docs/` into the root of your repository. Merge with existing files; do not overwrite established repository instructions without reviewing them.
2. Ask Copilot to perform **Milestone 0 — Repository discovery and architecture fit** first.
3. Review the architecture-fit report and agree on any changes required by the existing codebase.
4. For each later milestone, open `.github/prompts/implement-milestone.prompt.md`, replace `[MILESTONE_ID_AND_NAME]`, and ask Copilot to follow it.
5. Implement one milestone per PR. Keep the baseline benchmark and independent verifier intact throughout.

GitHub Copilot recognizes repository-wide instructions in `.github/copilot-instructions.md` and reusable prompt files under `.github/prompts/`. See the official GitHub documentation:
- https://docs.github.com/en/copilot/how-tos/copilot-on-github/customize-copilot/add-custom-instructions/add-repository-instructions
- https://docs.github.com/en/copilot/concepts/prompting-and-context/custom-instructions
