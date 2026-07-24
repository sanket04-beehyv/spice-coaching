# MicroCoaching Backend

MicroCoaching is implemented as a stateful platform service plus a private AI runtime inside one monorepo.

This document is the canonical reference for:
- the backend architecture
- the public and internal endpoint contract
- the current implementation status

Old drifted route names are not part of the contract and should not be used.

## Quick Start Docs

- Docs index and reading order: `docs/README.md`
- Setup issues and fixes: `docs/SETUP_TROUBLESHOOTING.md`
- Codebase map and ownership: `docs/PROJECT_NAVIGATION.md`

## Architecture

### Deployables

`platform-api`
- Public API for the Android SDK, admin flows, telemetry ingest, sync, and dashboards
- Owns PostgreSQL writes, ClickHouse writes, orchestration, and final response validation

`platform-worker`
- Async worker process from the same `platform_service` codebase
- Owns ingestion jobs, quiz jobs, and queued gap-profile updates

`ai-runtime`
- Private internal inference service
- Owns model-provider execution, embedding generation, and runtime response parsing
- Does not own PostgreSQL, ClickHouse, or public product workflows

### Shared Packages

`packages/contracts`
- Pydantic contracts, enums, DTOs

`packages/foundation`
- Shared config, logging, tracing, and lightweight infra helpers

### Ownership Rules

Platform owns:
- PostgreSQL schema and repositories
- Alembic migrations
- telemetry ingest and analytics writes
- sync contracts
- final coaching-card validation
- admin and dashboard APIs

AI runtime owns:
- provider adapters
- raw generation execution
- embedding generation
- runtime parsing

Shared packages do not own:
- SQLAlchemy models
- repositories
- business workflows
- domain services

## Repo Layout

```text
coaching-platform/
├── pyproject.toml
├── README.md
├── docker-compose.yml
├── infra/
│   ├── alembic/
│   └── alembic.ini
├── packages/
│   ├── contracts/
│   └── foundation/
└── services/
    ├── platform/
    │   └── src/platform_service/
    └── ai-runtime/
        └── src/ai_runtime/
```

## Canonical Endpoint Contract

### API root

Platform API routes are served under **`/medtronics-api`** (configurable via `API_ROOT_PATH`). Paths below are relative to that root; the full URL is `{api_root_path}` + path (e.g. `GET /medtronics-api/ready`, `POST /medtronics-api/coaching/counselling`).

### Authentication

When `SPICE_AUTH_ENABLED=true`, platform-api validates every request (except paths listed in `SPICE_AUTH_EXEMPT_PATHS`, default `ready`) by calling SPICE auth-service `POST {SPICE_AUTH_BASE_URL}/authenticate` with the caller's headers:

- `Authorization: Bearer <jwt>` (required)
- `client` (optional; defaults to `SPICE_AUTH_DEFAULT_CLIENT`, typically `mob` for the Android app)
- `auth-cookie` (optional; web clients)

Set `SPICE_AUTH_BASE_URL` to the auth-service root as seen by platform — either direct (`http://authservice:8089`) or via the nginx reverse proxy (`http://gateway/auth-service`).

Callers must obtain JWTs with the correct SPICE login client: admin web uses `client: web` (roles with `suiteAccessName: admin`); the Android SDK uses `client: mob` (roles with `suiteAccessName: mob`). Platform forwards the `client` header to `/authenticate` but authorization is based on roles embedded in the token.

### Authorization (two planes)

When SPICE auth is enabled, routes are split into **admin** and **device** planes (prefixes configurable via `SPICE_ADMIN_PATH_PREFIXES` and `SPICE_DEVICE_PATH_PREFIXES`):

| Plane | Path prefixes (under `API_ROOT_PATH`) | Who may call |
|-------|----------------------------------------|--------------|
| Admin | `admin`, `dashboard` | `isSuperUser`, or any role with `suiteAccessName == admin` |
| Device | `telemetry`, `sync`, `morning`, `coaching` | `isSuperUser`, `isJobUser`, or any role with `suiteAccessName == mob` |

Access is **strictly partitioned**: an admin principal cannot call device routes and vice versa, except `SUPER_USER` which may use both. Violations return `403` with `insufficient role for this API`.

Admin routes (including ingest, `POST /admin/ingest`) require **all** of: a valid SPICE token and admin-plane authorization when SPICE auth is enabled.

Default local/docker behavior keeps `SPICE_AUTH_ENABLED=false` so existing smoke tests work without a running auth-service.

### Platform API

v3.3 is **module-centric** (not scenario-centric). Device sync uses `/sync/*`; admin content management uses `/admin/modules/*` and `/admin/ingest`. Legacy scenario/counselling route names below are **not implemented** on platform-api — they remain in this doc only as historical context for the ai-runtime `GenerationType` enum.

#### Device-facing

`POST /coaching/rag-query`
- Embeds the question, runs pgvector similarity over published `module.embedding` rows, builds context from module cards, calls `ai-runtime` for a grounded JSON answer, and returns `source_document` rows with optional MinIO presigned URLs for PDF/source attribution
- Response includes `suggested_questions`: follow-up questions (in `response_language`) grounded in the retrieved module content

`POST /telemetry/events`
- Accepts telemetry batches from the SDK
- Writes analytics rows to ClickHouse
- Queues module-completion and gap-update jobs to Redis for background processing

`GET /sync/modules?since=<ISO-8601>&user_id=<int>`
- Returns published modules (with quiz payloads) updated after `since`
- When `user_id` is provided, also returns `assigned_module_ids` for that user (individual, po_sk, geographical, and group rules); when omitted or when the user has no assignments, `assigned_module_ids` is empty
- When `user_id` is provided, also returns `requested_modules` — the CHW's full training-request history (`module_id` and/or free-text `requested_module_name`, optional `reason`, `submitted_at`); when omitted, `requested_modules` is empty

`GET /sync/triggers?since=<ISO-8601>`
- Returns trigger definitions updated after `since`

`GET /sync/gaps?chw_id=<int>&since=<ISO-8601>`
- Returns behavioural gaps, CHW gap states, module completions, and partial quiz progress for offline device use

`GET /sync/chat-faqs?since=<ISO-8601>`
- Returns 5–6 ranked FAQ suggestion chips as locale-keyed maps (`question: {"bn": "...", "en": "..."}`) synthesized nightly from clustered `digital_help_used` telemetry for the resolved tenant

`GET /sync/config`
- Returns current config thresholds and deployment `locales` (`primary`, `supported`) for device sync

`POST /sync/source-documents/presigned-urls`
- Batch presigned GET URLs for source documents (max 50 IDs per request)

`POST /sync/source-documents/presigned-thumbnails`
- Batch presigned GET URLs for source document thumbnails (max 50 IDs per request)

`GET /sync/source-documents/published`
- Return presigned GET URLs (and thumbnail URLs) for source documents linked to published modules where `sync_published_visible=true`; module payloads come from `GET /sync/modules`

`POST /sync/modules/presigned-thumbnails`
- Batch presigned GET URLs for module thumbnails (max 50 IDs per request)

`GET /morning/cards?chw_id=<int>`
- Returns prioritized module IDs for morning review
- Uses the configured `morning_cards_max` threshold

Module training requests (CHW self-service access) are accepted via `POST /telemetry/events` with `event_type=module_requested` (top-level `module_id` and/or `payload_json.requested_module_name`, optional `payload_json.reason`). See `docs/TELEMETRY_CONTRACT.md`.

#### Admin-facing

`POST /admin/ingest`
- Uploads one or more source files (multipart field `files`, max 10) to MinIO synchronously, then enqueues the v3.3 pipeline (A→B→C→D) per file on `platform-celery-worker`
- Form fields: optional `titles` (JSON array, one title per file in order; if omitted, each title is the file’s basename stem), optional `override_duplicates` (JSON array of booleans, one per file — when `true`, re-ingest even if the file’s `content_sha256` matches an already-`ingested` `source_document`), optional `sync_published_visible` (JSON array of booleans, one per file — when `true`, the source document may appear in `GET /sync/source-documents/published`; default all `false`), optional `ingestion_instructions` (batch-wide steering text for Stage C module identification; sanitized at ingest), optional `cards_per_module` and `quizzes_per_module` (fixed card/quiz counts per module for this ingest; must fall within deployment bounds), `fuse_sources` (default `false` — when `true`, runs cross-source fusion after all pipelines finish; requires ≥2 successfully ingested files), `skip_merge` (default `false` — when `true`, Stage D always creates new modules and does not merge into existing ones), plus `content_domain`, `assessment_mode`; primary language is always the deployment primary locale
- Duplicate detection uses SHA256 of file bytes against `source_document` rows with `status='ingested'` only (`failed` / `ingesting` do not block)
- Returns `202` with `status: batch_queued`, `sources[]` (each with `source_document_id`, `poll_url`, etc.), and optionally `skipped_duplicates[]` when some files were blocked; returns `409` with `detail.code=duplicate_content` when every file is blocked; poll `GET /admin/ingest/by-document/{id}` for progress

`GET /admin/ingest/by-document/{source_document_id}`
- Polls the most recent ingestion run for a source document (steps + candidates). Response may include `run_kind`, `current_activity` (e.g. published-module merge or cross-source fusion in progress), `published_module_merge` on completed `card_draft` steps, and a nested `cross_source_fusion` block when batch fusion is running for this document.

`POST /admin/files`
- Upload an admin file asset (MinIO)

`GET /admin/files/presigned-url`
- Presigned GET for an admin file object

`POST /admin/modules` and `GET /admin/modules`
- Create and list published modules. List returns a paginated envelope:
  `{ modules, total_modules, total_pages, limit, offset }`

`GET /admin/modules/domains`
- Distinct `module.domain` values for admin filter dropdowns; optional `status` matches the modules list tabs

`GET /admin/modules/{module_id}`, `PUT /admin/modules/{module_id}`, `DELETE /admin/modules/{module_id}`
- Module CRUD. `PUT` requires `expected_version` (the version of the module row being edited). If that version is stale or another writer already created a newer family tip, returns `409` with `detail.code=module_version_conflict` (`expected_version`, `current_version`, `latest_module_id`); client must `GET` the latest module and retry. When the body is a **complete content snapshot** (`title`, `description`, `module_json`, `thumbnail_storage_path`, plus quiz as top-level `quiz` or nested `module_json.quiz`) and matches the tip, `PUT` is a no-op and returns the existing `id` / `version` (no new draft). `chatbot_faqs_only`, gap ids, and `editor_id` are ignored for equality. Omitted content fields still create a new version.

`GET /admin/modules/search`
- Search modules by title/content filters

`POST /admin/modules/{module_id}/regenerate-quiz` and `POST /admin/modules/{module_id}/regenerate-embedding`
- Enqueue post-publish quiz or embedding regeneration

`POST /admin/trigger-bindings` and related CRUD under `/admin/trigger-bindings/*`
- Manage trigger-to-module bindings

`GET /admin/ingestion-runs` and `GET /admin/ingestion-runs/{run_id}`
- List and inspect ingestion runs

`GET /admin/source-documents`
- List source documents for admin catalog views (ingest dropdowns, video upload table). Optional `status` (`ingesting` | `ingested` | `failed`; default `ingested`), optional `source_type` (`pdf` | `pptx` | `docx` | `audio` | `video`; repeat and/or comma-separate for multiple), optional `q` (case-insensitive substring on `original_filename` or `title`); supports `limit` (default 50, max 200) and `offset` (default 0). Returns a paginated envelope: `{ source_documents, total_source_documents, total_pages, limit, offset }`

`GET /admin/module-demand/summary`
- LLM narrative plus top-K form + chatbot (`digital_help_used`, keyed on `module_id`) demand, split into available (assign / open draft) and unavailable (create); K from config key `module_demand_top_k`. Served from a daily-precomputed snapshot (Celery beat `platform.refresh_module_demand_summary`), with a live-build fallback on cache miss

`GET /admin/module-demand/modules/{module_id}/requestors`
- Form requestors for a module (source `form` in V1), with `already_assigned` from individual assignments

`POST /admin/module-demand/modules/{module_id}/assign`
- Bulk individual assign from the demand flow (served from the assignments router); records `module_demand_assigned` attribution audit


#### Dashboard-facing

`GET /dashboard/supervisor/{chw_id}`

`GET /dashboard/district/{upazila_id}`

`GET /dashboard/llm-quality`

`GET /dashboard/digital-help-modules`

Current state:
- these routes are part of the canonical API surface
- `GET /dashboard/supervisor/{chw_id}` reads from the ClickHouse `chw_daily_summary` materialized view (see `infra/clickhouse/init.sql`)
- `GET /dashboard/llm-quality` queries ClickHouse when `llm_daily_summary` exists in the deployment
- `GET /dashboard/digital-help-modules` ranks modules by `digital_help_used` event volume over `period_days` (default 30), keyed on concrete `module_id` (events without `module_id` ignored; no family roll-up), enriched with module titles from PostgreSQL; supports `limit` (default 20) and `offset` (default 0) pagination with `total_modules` in the response
- Admin module demand (`GET /admin/module-demand/summary`) soft-fails chatbot CH merge so form demand still returns
- `GET /dashboard/district/{upazila_id}` still returns `501 Not Implemented` until implemented

#### Operational

`GET /ready`
- Readiness probe: PostgreSQL, Redis, ClickHouse, ai-runtime (including provider alignment), and object storage

### AI Runtime

#### Internal-only

`POST /internal/generate/{generation_type}`
- Canonical private generation endpoint
- Supported generation types are validated against the shared enum
- This intentionally consolidates counselling, IT help, extraction, and quiz generation into one internal route

`POST /internal/embed`
- Private embedding endpoint used by platform ingestion/retrieval flows

`GET /health`
- Basic runtime liveness endpoint

## Request Flows

### Coaching RAG

1. SDK calls `POST /coaching/rag-query`
2. Platform embeds the question via `POST /internal/embed` on `ai-runtime`
3. Platform retrieves similar published modules from pgvector
4. Platform builds a grounded prompt and calls `POST /internal/generate/coaching_rag`
5. Platform returns the JSON answer with source-document attribution

### Telemetry

1. SDK calls `POST /telemetry/events`
2. Platform validates and translates telemetry rows
3. Platform writes accepted telemetry to ClickHouse
4. Platform queues module-completion and gap-update jobs to Redis
5. Worker consumes queued jobs and updates PostgreSQL state

### Sync

1. SDK calls `GET /sync/modules?since=...` for published modules and quizzes (optional `user_id` for `assigned_module_ids` and `requested_modules`)
2. SDK calls `GET /sync/triggers?since=...` and `GET /sync/gaps?chw_id=...&since=...` as needed
3. SDK calls `GET /sync/chat-faqs?since=...` for bilingual FAQ suggestion chips (clustered + LLM-synthesized nightly)
4. SDK calls `GET /sync/config` for threshold/config values
5. SDK uses presign endpoints for offline PDF/thumbnail access

## Current Implementation Status

### Implemented

- monorepo layout with `platform`, `ai-runtime`, `contracts`, and `foundation`
- single Alembic chain under `infra/alembic`
- v3.3 module-centric sync (`/sync/modules`, `/sync/triggers`, `/sync/gaps`, `/sync/chat-faqs`, presign batches)
- coaching RAG via platform → internal AI runtime
- telemetry ingest with ClickHouse writes
- Redis-backed Celery workers for:
  - v3.3 ingest pipeline (A→B→C→D) and cross-source fusion
  - post-publish quiz, embedding, and gap classification
  - module-completion telemetry processing
- morning-card selection with config-driven `morning_cards_max`
- SPICE auth middleware with admin/device authorization planes (when `SPICE_AUTH_ENABLED=true`)
- `/ready` readiness probe with dependency checks

### Intentionally Designed This Way

- `ai-runtime` uses one generic internal generation route:
  - `POST /internal/generate/{generation_type}`
- This is an intentional consolidation, not route drift

### Not Yet Fully Implemented

- `GET /dashboard/district/{upazila_id}` (returns `501`)
- legacy device routes: `POST /coaching/counselling`, `POST /coaching/quiz-answer`, `POST /coaching/it-help`
- legacy sync route: `GET /scenarios/sync?since_version=N` (replaced by `GET /sync/modules?since=...`)
- legacy admin scenario/document routes under `/admin/scenarios/*` and `/admin/documents/*`
- full config-management admin endpoints beyond threshold sync
- richer dashboard and analytics materialization flows

## Standards Decisions

- No public generic chat or unrestricted RAG endpoints are exposed
- The SDK talks only to `platform-api`
- `ai-runtime` is private and token-protected
- Platform is the system of record
- Legacy drifted route aliases are not part of the API contract
- The workspace is managed with `uv`, and `uv.lock` should be committed

## Local Development

### Prerequisites (first-run)

```bash
cp .env.example .env           # then fill GOOGLE_API_KEY
uv sync --locked --all-packages --group dev
```

Without `.env`, `docker compose` fails at parse time because `GOOGLE_API_KEY` is declared required. See `docs/SETUP_TROUBLESHOOTING.md` for other known failures.

### Pre-commit

After syncing dev dependencies, install git hooks once:

```bash
uv run pre-commit install
```

Hooks run ruff (lint + format), Pyright type checking (on Python changes), and basic file hygiene on each commit. To check the whole repo without committing:

```bash
uv run pre-commit run --all-files
```

### Environment

### Run services

Platform API:

```bash
uv run uvicorn platform_service.main:app --host 0.0.0.0 --port 8000
```

Platform worker:

```bash
uv run python -m platform_service.workers.main
```

AI runtime:

```bash
uv run uvicorn ai_runtime.main:app --host 0.0.0.0 --port 8001
```

### Run with Docker Compose

1. Create a root `.env` file (or export variables in your shell) and set:
   - `GOOGLE_API_KEY` (required for `ai-runtime`; `GEMINI_API_KEY` is accepted as fallback)
   - `AI_RUNTIME_TOKEN` (optional, defaults to `dev-internal-token`)
   - `APP_ENV` (optional, defaults to `development`)
2. Start the stack:

```bash
docker compose up --build
```

Expected compose behavior:
- `migrate` runs once and exits with code `0`
- `clickhouse-init` runs once and exits with code `0`
- `db`, `redis`, `clickhouse`, `ai-runtime`, and `platform-api` are running
- `platform-celery-worker` and `platform-celery-beat` are running

Quick verification:

```bash
docker compose ps
curl -fsS http://localhost:8000/medtronics-api/ready
curl -fsS http://localhost:8001/health
```

### Migrations

Run Alembic as a separate step:

```bash
uv run alembic -c infra/alembic.ini upgrade head
```

Migrations should not be auto-run by application startup.

## Next Recommended Work

1. Implement `GET /dashboard/district/{upazila_id}`.
2. Add rate limiting on device-plane routes.
3. Add sync pagination for large module catalogs.
4. Add remaining planned admin config-management flows.
5. Extend CI with type-checking beyond ruff (pyright/mypy) where practical.
