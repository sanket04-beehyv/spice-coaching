# Claude Code / AI Assistant Guide

This file is the entry point for Claude Code, Cursor, and similar AI assistants working in this repo.

## Read these first

1. `README.md` — architecture and canonical endpoint contract.
2. `docs/SETUP_TROUBLESHOOTING.md` — known setup issues and verified fixes.
3. `docs/error-codes.json` — client-facing error code catalogue. When adding, removing, or renaming `mc_contracts.errors.ErrorCode`, update this file in the same change; pre-commit (`check-error-codes-catalog`) enforces parity. Problem Details `type` is `docs/error-codes.json#{code}`.

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
- Docker service images are `python:3.12-slim` with no `curl` — healthchecks use the Python urllib probe in compose/Dockerfiles.
