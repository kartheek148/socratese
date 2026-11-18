# ADR 0001 — Modular monolith on the greenfield default stack

Status: Proposed (Milestone 0)

## Context
The repository contains only design documents; there is no existing code, dependency manifest, database, queue, or CI to reuse.

## Decision
Build a single Python 3.12 package (`src/experience_engine`) managed with `uv`, using the defaults from `.github/copilot-instructions.md`: Pydantic, FastAPI, PostgreSQL, SQLAlchemy/Alembic, RabbitMQ/Celery. pgvector and LangGraph are deferred until a milestone shows measured need. Dependencies are added only in the milestone that needs them (M1: pydantic, pytest, ruff, mypy; M2: SQLAlchemy, Alembic, psycopg, Celery, FastAPI).

## Consequences
- No microservices, graph DB, or online fine-tuning.
- Pure packages (`domain`, `ports`, `execution`, `learning`, `capabilities`, `bench`) never import framework adapters; an architecture test enforces this.
- Provider, vector store, object store and orchestration framework sit behind `ports/` Protocols.
