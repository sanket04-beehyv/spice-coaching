# Project Navigation Guide

Use this map to quickly find where to work based on the task you are doing.

## Monorepo Structure

```text
coaching-platform/
├── infra/
│   ├── alembic/
│   └── alembic.ini
├── packages/
│   ├── contracts/
│   └── foundation/
├── services/
│   ├── platform/
│   └── ai-runtime/
└── docs/
```

## What Lives Where

### `services/platform`
- Public API routes (device, admin, dashboard).
- Domain workflows and orchestration.
- Redis queue producers/consumers and worker entrypoints.
- PostgreSQL and ClickHouse integration logic.

### `services/ai-runtime`
- Internal-only inference endpoints.
- Provider adapters and generation pipelines.
- Embedding generation and runtime-level parsing.

### `packages/contracts`
- Shared DTOs, enums, and API contracts used by both services.

### `packages/foundation`
- Shared config, logging, tracing, and helper utilities.

### `infra/alembic`
- Migration scripts and Alembic environment wiring.

### `docs`
- Operational setup guidance and contributor navigation docs.

## Where To Start By Task

### Add or change a public endpoint
1. Start in `services/platform/src/platform_service`.
2. Update route handlers and service logic.
3. Update shared request/response contracts in `packages/contracts` if needed.
4. Verify via `http://localhost:8000/docs`.

### Update AI generation behavior
1. Start in `services/ai-runtime/src/ai_runtime`.
2. Modify provider adapters, prompts, or generation pipelines.
3. Validate with runtime health and internal generation requests.

### Change schema or persistence behavior
1. Add migration in `infra/alembic/versions`.
2. Update platform models/repositories in `services/platform`.
3. Run `alembic -c infra/alembic.ini upgrade head`.

### Change queues or background jobs
1. Start in `services/platform/src/platform_service/workers`.
2. Keep queue names and payload contracts aligned with producers.
3. Verify worker startup logs and queue consumption behavior.

### Update shared contracts or framework utilities
1. Start in `packages/contracts` or `packages/foundation`.
2. Validate both `platform` and `ai-runtime` still run with the updated package.

## Suggested Documentation Coverage

Keep these docs present and current:
- `README.md` (root): canonical architecture and endpoint contract.
- `docs/README.md`: index page and doc-maintenance expectations.
- `docs/SETUP_TROUBLESHOOTING.md`: setup pain points and fixes.
- `docs/PROJECT_NAVIGATION.md`: where to implement changes by task.
