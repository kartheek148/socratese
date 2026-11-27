# Experience Engine Copilot Blueprint

This package contains a repository-ready starting point for implementing an Experience-Driven Self-Improving Agent.

## Contents

- `.github/copilot-instructions.md` — always-on repository guidance for GitHub Copilot.
- `.github/prompts/implement-milestone.prompt.md` — reusable prompt to implement exactly one milestone at a time.
- `docs/architecture.md` — system architecture, components, interfaces, data model, execution flows, safety, and measurement.
- `docs/implementation-roadmap.md` — milestone plan and acceptance criteria.

## Development (Milestones 1-5)

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

Benchmark and baseline: [docs/benchmark.md](docs/benchmark.md). Data model: [docs/schema.md](docs/schema.md). Events, outbox and messaging: [docs/events.md](docs/events.md). Capabilities, DSL and the candidate compiler (`ee-learn`): [docs/capabilities.md](docs/capabilities.md). Evaluation, promotion policy and the registry (`ee-eval`): [docs/evaluation.md](docs/evaluation.md). Runtime capability matching, composition and fallback: [docs/runtime.md](docs/runtime.md). Capability-vs-memory experiment (`ee-exp`): [docs/experiments/capability-vs-memory-design.md](docs/experiments/capability-vs-memory-design.md), results in [docs/evaluations/capability-vs-memory-report.md](docs/evaluations/capability-vs-memory-report.md). Governed memory and the gated memory-vs-capability comparison (`ee-gov`, `ee-exp --mode gated`): [docs/experiments/gated-memory-vs-capability-design.md](docs/experiments/gated-memory-vs-capability-design.md), results in [docs/evaluations/gated-memory-vs-capability-report.md](docs/evaluations/gated-memory-vs-capability-report.md). Promotion-policy audit, the candidate risk-based policy and cheaper validation (`--policy-set risk-v2`, `--baseline-cache`, `ee-policy`): [docs/experiments/promotion-policy-v2-design.md](docs/experiments/promotion-policy-v2-design.md), results in [docs/evaluations/promotion-policy-confirmation-report.md](docs/evaluations/promotion-policy-confirmation-report.md). Production-aligned evaluation (capability vs. fallback attribution, `--attribute-phases`, `ee-exp serving-report`): [docs/experiments/production-aligned-evaluation-design.md](docs/experiments/production-aligned-evaluation-design.md), results in [docs/evaluations/production-aligned-learning-evaluation.md](docs/evaluations/production-aligned-learning-evaluation.md). Adaptive strategy controller (`ee-ctl`: cost-aware / bandit policies, calibration, offline-shadow-canary rollout with rollback): [docs/controller.md](docs/controller.md), design [docs/experiments/adaptive-controller-design.md](docs/experiments/adaptive-controller-design.md), results in [docs/evaluations/adaptive-controller-report.md](docs/evaluations/adaptive-controller-report.md). Evidence-driven controller (M6.1: paired validation priors, bounded exploration): design [docs/experiments/evidence-driven-controller-design.md](docs/experiments/evidence-driven-controller-design.md), results in [docs/evaluations/evidence-driven-controller-report.md](docs/evaluations/evidence-driven-controller-report.md), ADR [docs/decisions/0016-evidence-driven-controller.md](docs/decisions/0016-evidence-driven-controller.md). Evidence reliability and drift monitoring (M6.2): design [docs/experiments/evidence-reliability-design.md](docs/experiments/evidence-reliability-design.md), results in [docs/evaluations/evidence-reliability-report.md](docs/evaluations/evidence-reliability-report.md), ADR [docs/decisions/0017-evidence-reliability-and-drift-monitor.md](docs/decisions/0017-evidence-reliability-and-drift-monitor.md). Controlled revalidation and recovery (M6.3: audit [docs/research/m6_3_revalidation_audit.md](docs/research/m6_3_revalidation_audit.md), design [docs/experiments/revalidation-design.md](docs/experiments/revalidation-design.md), results in [docs/evaluations/revalidation-report.md](docs/evaluations/revalidation-report.md), ADR [docs/decisions/0018-controlled-revalidation.md](docs/decisions/0018-controlled-revalidation.md)).

## How to use

1. Copy `.github/` and `docs/` into the root of your repository. Merge with existing files; do not overwrite established repository instructions without reviewing them.
2. Ask Copilot to perform **Milestone 0 — Repository discovery and architecture fit** first.
3. Review the architecture-fit report and agree on any changes required by the existing codebase.
4. For each later milestone, open `.github/prompts/implement-milestone.prompt.md`, replace `[MILESTONE_ID_AND_NAME]`, and ask Copilot to follow it.
5. Implement one milestone per PR. Keep the baseline benchmark and independent verifier intact throughout.

GitHub Copilot recognizes repository-wide instructions in `.github/copilot-instructions.md` and reusable prompt files under `.github/prompts/`. See the official GitHub documentation:
- https://docs.github.com/en/copilot/how-tos/copilot-on-github/customize-copilot/add-custom-instructions/add-repository-instructions
- https://docs.github.com/en/copilot/concepts/prompting-and-context/custom-instructions
