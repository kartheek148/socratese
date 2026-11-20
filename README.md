# Experience Engine Copilot Blueprint

This package contains a repository-ready starting point for implementing an Experience-Driven Self-Improving Agent.

## Contents

- `.github/copilot-instructions.md` — always-on repository guidance for GitHub Copilot.
- `.github/prompts/implement-milestone.prompt.md` — reusable prompt to implement exactly one milestone at a time.
- `docs/architecture.md` — system architecture, components, interfaces, data model, execution flows, safety, and measurement.
- `docs/implementation-roadmap.md` — milestone plan and acceptance criteria.

## Development (Milestones 1-2)

```
uv sync
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run pytest                      # unit/regression/architecture; integration + migration skip without infra

# Integration + migration tests need PostgreSQL and RabbitMQ (Docker):
EE_PG_PORT=55432 EE_AMQP_PORT=56720 docker compose -f deploy/docker-compose.yml -p ee-test up -d --wait
export EE_TEST_DATABASE_URL=postgresql+psycopg://ee:ee_dev_password@127.0.0.1:55432/postgres
export EE_TEST_AMQP_URL=amqp://ee:ee_dev_password@127.0.0.1:56720//
uv run pytest                      # creates/drops its own database and uniquely named queues
docker compose -f deploy/docker-compose.yml -p ee-test down
```

Benchmark and baseline: [docs/benchmark.md](docs/benchmark.md). Data model: [docs/schema.md](docs/schema.md). Events, outbox and messaging: [docs/events.md](docs/events.md). Capabilities, DSL and the candidate compiler (`ee-learn`): [docs/capabilities.md](docs/capabilities.md). Evaluation, promotion policy and the registry (`ee-eval`): [docs/evaluation.md](docs/evaluation.md).

## How to use

1. Copy `.github/` and `docs/` into the root of your repository. Merge with existing files; do not overwrite established repository instructions without reviewing them.
2. Ask Copilot to perform **Milestone 0 — Repository discovery and architecture fit** first.
3. Review the architecture-fit report and agree on any changes required by the existing codebase.
4. For each later milestone, open `.github/prompts/implement-milestone.prompt.md`, replace `[MILESTONE_ID_AND_NAME]`, and ask Copilot to follow it.
5. Implement one milestone per PR. Keep the baseline benchmark and independent verifier intact throughout.

GitHub Copilot recognizes repository-wide instructions in `.github/copilot-instructions.md` and reusable prompt files under `.github/prompts/`. See the official GitHub documentation:
- https://docs.github.com/en/copilot/how-tos/copilot-on-github/customize-copilot/add-custom-instructions/add-repository-instructions
- https://docs.github.com/en/copilot/concepts/prompting-and-context/custom-instructions
