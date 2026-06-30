# Claude Code / AI Assistant Guide

This file is the entry point for Claude Code, Cursor, and similar AI assistants working in this repo. It points at the authoritative rules so no guidance lives in two places.

## Read these first

1. `README.md` — architecture and canonical endpoint contract.
2. `.cursor/rules/repo-overview.mdc` — ownership graph and where to put new code.
3. `.cursor/rules/python-standards.mdc` — coding standards, tooling, logging.
4. `.cursor/rules/migrations-and-db.mdc` — Alembic and repository rules.
5. `.cursor/rules/local-setup.mdc` — docker/compose/Dockerfile rules.
6. `.cursor/rules/no-inline-imports.mdc` — strict rule for no inline imports
6. `docs/SETUP_TROUBLESHOOTING.md` — known setup issues and verified fixes.

## Before you commit

Preferred (runs automatically after `uv run pre-commit install`):

```bash
uv run pre-commit run --all-files
```

Manual equivalent:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright
```

If a rule blocks a task and you think the rule is wrong, **flag it explicitly in the PR description** rather than silently working around it. Rules that are worked around silently rot.

## Boundaries that matter most

- `services/ai-runtime` is stateless. No DB, no Redis, no ClickHouse.
- `services/platform` never imports an LLM SDK directly. All AI calls go through `AIRuntimeClient`.
- Alembic never runs at app startup — it's a separate step.
- Docker service images are `python:3.12-slim` with no `curl` — healthchecks use the Python urllib probe (see local-setup rule).
